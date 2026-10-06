"""Start the pinned ACE-Step API server with Apple Silicon fixes.

Run with the ACE runtime's own interpreter (`.runtime/ace-step-1.5/.venv/bin/python`);
`scripts/start_ace_api.sh` does this. Arguments are passed to ACE's own server CLI.
The pinned checkout itself is not modified.

1. Seeded LM planning. With `thinking=true` the 5Hz LM samples the song's audio codes
(melody, structure, vocal phrasing) before the DiT renders them. For one song per
request (this engine always sends `batch_size=1`), ACE v0.1.8's MLX path never seeds
the LM sampler, so the same seed produced a different song on every call (measured:
max sample difference 1.30 between two identical requests; DiT-only requests were
bit-identical). The seed recorded in `project.json` then did not identify a version.
The LM sampler is now seeded from the request seed.

2. Memory. On macOS, ACE loads the DiT in float32 on MPS, then converts a second float32 copy
into the native MLX decoder that actually runs diffusion (text2music and repaint).
The PyTorch copy stays resident only as a fallback for an MLX failure and for
LoRA/scoring features this engine does not call. For the XL (4B) DiT that is about
20 GB of idle weights; measured on a 36 GB M4 Max, the server's footprint reached
47 GB and macOS terminated it mid-generation.

Freeing the PyTorch decoder after it reached MPS is not enough: measured with XL, the
tensors were released (MPS allocated 19.95 -> 3.28 GB) but the MPS driver kept 17.29 GB
even after `torch.mps.empty_cache()`. The decoder is therefore converted while the
checkpoint is still memory-mapped on the CPU, right after `from_pretrained` and before
ACE moves the model to MPS, one tensor at a time. Only the encoder, tokenizer and
detokenizer reach MPS. Afterwards the PyTorch decoder moves to the meta device. If MLX
initialisation fails, the server refuses to start instead of falling back to the
emptied PyTorch decoder.

3. MLX DiT precision. `MUSIC_ENGINE_ACE_MLX_DIT_DTYPE` is `float32`, `bfloat16` or
`auto` (default). `auto` keeps float32 for decoders up to 3B parameters (turbo: 1.6B,
unchanged output) and stores the matrix weights of larger decoders (XL: 4.2B, 16.7 GB
in float32) in bfloat16, which is the precision ACE itself uses for these checkpoints
on CUDA. Norms, biases and modulation tables stay float32 and activations are float32.

4. CFG guidance on MLX. SFT/base checkpoints use APG guidance. ACE's PyTorch path keeps
a momentum buffer `running = diff + (-0.75) * running`; the pinned MLX port computes
`running = diff + running`, so every step adds all earlier guidance differences.
The MLX step now uses the PyTorch momentum. Turbo requests do not use CFG.

5. DCW for SFT/base. The pinned REST API has no `dcw_enabled` field, so every request
inherits `GenerationParams.dcw_enabled=True`. Upstream found that this distorts
non-turbo checkpoints (ACE-Step issue #1259, fixed in #1282 on 2026-08-28 by defaulting
DCW off unless the loaded model's `config.is_turbo`), and the Gradio UI already did so.
The same rule applies here; turbo keeps DCW and its output is unchanged.
"""

from __future__ import annotations

import gc
import argparse
import os
import sys
from pathlib import Path

# ACE's own default in acestep/models/common/apg_guidance.py (MomentumBuffer).
APG_MOMENTUM = -0.75
BFLOAT16_DECODER_PARAMETERS = 3_000_000_000


def mlx_dit_dtype(decoder_parameters: int) -> str:
    requested = os.environ.get("MUSIC_ENGINE_ACE_MLX_DIT_DTYPE", "auto").strip().lower()
    if requested not in {"auto", "float32", "bfloat16"}:
        raise ValueError("MUSIC_ENGINE_ACE_MLX_DIT_DTYPE must be auto, float32 or bfloat16")
    if requested == "auto":
        return "bfloat16" if decoder_parameters > BFLOAT16_DECODER_PARAMETERS else "float32"
    return requested


def convert_decoder_on_load() -> None:
    import mlx.core as mx
    import torch
    import transformers
    from loguru import logger

    from acestep.models.mlx import dit_convert

    def stream_decoder(pytorch_model):
        decoder = pytorch_model.decoder
        parameters = sum(parameter.numel() for parameter in decoder.parameters())
        dtype = mlx_dit_dtype(parameters)
        on_mps = any(parameter.device.type == "mps" for parameter in decoder.parameters())
        pytorch_model._local_music_engine_decoder = {"parameters": parameters, "mlxDtype": dtype}
        weights = []
        freed = 0
        # keep_vars returns the live tensors, so replacing `.data` frees each one.
        for key, tensor in decoder.state_dict(keep_vars=True).items():
            new_key = key
            value = tensor.detach().cpu().float().numpy()
            # Same layout changes as dit_convert.convert_decoder_weights.
            if key.startswith("proj_in.1."):
                new_key = key.replace("proj_in.1.", "proj_in.")
                if new_key.endswith(".weight"):
                    value = value.swapaxes(1, 2)
            elif key.startswith("proj_out.1."):
                new_key = key.replace("proj_out.1.", "proj_out.")
                if new_key.endswith(".weight"):
                    value = value.transpose(1, 2, 0)
            elif "rotary_emb" in key:
                continue
            array = mx.array(value)
            if dtype == "bfloat16" and array.ndim == 2:
                array = array.astype(mx.bfloat16)
            mx.eval(array)
            weights.append((new_key, array))
            del value
            freed += tensor.numel() * tensor.element_size()
            tensor.data = torch.empty(0, dtype=tensor.dtype, device=tensor.device)
            if freed >= 1 << 30:
                gc.collect()
                if on_mps:
                    torch.mps.empty_cache()
                freed = 0
        gc.collect()
        if on_mps:
            torch.mps.empty_cache()
        logger.info(
            "[local-music-engine] Streamed {:.2f}B DiT parameters from {} into MLX ({} matrix weights); "
            "each PyTorch tensor was freed after conversion.",
            parameters / 1e9,
            "MPS" if on_mps else "the CPU checkpoint",
            dtype,
        )
        return weights

    load_model = transformers.AutoModel.from_pretrained

    def from_pretrained(*args, **kwargs):
        model = load_model(*args, **kwargs)
        # ACE also loads its text encoder through AutoModel; only the DiT has this class.
        if type(model).__name__ == "AceStepConditionGenerationModel":
            model._local_music_engine_mlx_weights = stream_decoder(model)
        return model

    def convert_decoder_weights(pytorch_model):
        weights = pytorch_model.__dict__.pop("_local_music_engine_mlx_weights", None)
        return weights if weights is not None else stream_decoder(pytorch_model)

    transformers.AutoModel.from_pretrained = from_pretrained
    dit_convert.convert_decoder_weights = convert_decoder_weights


def release_torch_decoder_after_mlx_init() -> None:
    import torch
    from loguru import logger

    from acestep.core.generation.handler.mlx_dit_init import MlxDitInitMixin

    original = MlxDitInitMixin._init_mlx_dit

    def init_mlx_dit(self, compile_model: bool = False) -> bool:
        ready = original(self, compile_model)
        model = getattr(self, "model", None)
        if not ready and hasattr(model, "_local_music_engine_decoder"):
            # ACE treats MLX failure as non-fatal and would fall back to the
            # PyTorch decoder, whose tensors the streamed conversion already freed.
            raise RuntimeError("MLX DiT initialisation failed after the PyTorch decoder was released")
        decoder = getattr(model, "decoder", None)
        if not ready or decoder is None:
            return ready
        decoder.to("meta")
        gc.collect()
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
        logger.info(
            "[local-music-engine] Released the PyTorch DiT decoder {}; "
            "MLX runs diffusion and the PyTorch fallback is disabled.",
            getattr(self.model, "_local_music_engine_decoder", {}),
        )
        return ready

    MlxDitInitMixin._init_mlx_dit = init_mlx_dit


def match_pytorch_apg_momentum() -> None:
    from acestep.models.mlx import dit_generate

    def apg_forward(pred_cond, pred_uncond, guidance_scale, momentum_state=None, norm_threshold=2.5):
        import mlx.core as mx

        proj_axis = 1
        diff = pred_cond - pred_uncond
        if momentum_state is not None:
            diff = diff + APG_MOMENTUM * momentum_state.get("running", 0)
            momentum_state["running"] = diff
        if norm_threshold > 0:
            diff_norm = mx.sqrt((diff * diff).sum(axis=proj_axis, keepdims=True))
            scale_factor = mx.minimum(mx.ones_like(diff_norm), norm_threshold / (diff_norm + 1e-8))
            diff = diff * scale_factor
        v1 = pred_cond / (mx.sqrt((pred_cond * pred_cond).sum(axis=proj_axis, keepdims=True)) + 1e-8)
        parallel = (diff * v1).sum(axis=proj_axis, keepdims=True) * v1
        return pred_cond + (guidance_scale - 1) * (diff - parallel)

    dit_generate._mlx_apg_forward = apg_forward


def disable_dcw_for_non_turbo() -> None:
    import functools

    from loguru import logger

    from acestep.core.generation.handler.generate_music import GenerateMusicMixin

    original = GenerateMusicMixin.generate_music

    # wraps keeps the signature: ACE drops every kwarg a handler's signature lacks.
    @functools.wraps(original)
    def generate_music(self, *args, **kwargs):
        if not self.is_turbo_model() and kwargs.get("dcw_enabled"):
            kwargs["dcw_enabled"] = False
            logger.info("[local-music-engine] DCW disabled for the non-turbo DiT (ACE-Step #1259).")
        return original(self, *args, **kwargs)

    GenerateMusicMixin.generate_music = generate_music


def seed_lm_sampler_from_request() -> None:
    from acestep.llm_inference import LLMHandler

    original = LLMHandler.generate_with_stop_condition

    def generate_with_stop_condition(self, *args, **kwargs):
        seeds = kwargs.get("seeds")
        if seeds and self.llm_backend == "mlx":
            import mlx.core as mx

            # Batch requests reseed per item inside ACE; this covers the single-item
            # path, which calls the sampler without any seed.
            mx.random.seed(int(seeds[0]) % 2**32)
        return original(self, *args, **kwargs)

    LLMHandler.generate_with_stop_condition = generate_with_stop_condition


def main() -> None:
    # This wrapper shares only the engine's stdlib auth/boundary code, not its venv.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from local_music_engine.ace_auth import ensure_api_key
    from local_music_engine.ace_security import MusicApiGuard

    options = argparse.ArgumentParser(add_help=False)
    options.add_argument("--api-key")
    options.add_argument("--host", default=os.environ.get("ACESTEP_API_HOST", "127.0.0.1"), choices=["127.0.0.1", "localhost", "::1"])
    supplied, _ = options.parse_known_args()
    if supplied.api_key is not None:
        os.environ["MUSIC_ENGINE_ACE_API_KEY"] = supplied.api_key
    api_key = ensure_api_key()
    os.environ["ACESTEP_API_KEY"] = api_key
    mlx_dit_dtype(0)  # reject an invalid setting before any model loads
    seed_lm_sampler_from_request()
    convert_decoder_on_load()
    release_torch_decoder_after_mlx_init()
    match_pytorch_apg_momentum()
    disable_dcw_for_non_turbo()
    from acestep import api_server

    # Uvicorn loads this module's already-created app in the same (single) worker.
    api_server.app = MusicApiGuard(api_server.app, api_key)

    api_server.main()


if __name__ == "__main__":
    main()
