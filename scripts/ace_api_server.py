"""Start the pinned ACE-Step API server with two Apple Silicon fixes.

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

Once MLX is initialised, the PyTorch decoder moves to the meta device. If MLX later
fails, ACE's PyTorch fallback raises and the job is recorded as failed instead of
running on a second copy.
"""

from __future__ import annotations

import gc


def release_torch_decoder_after_mlx_init() -> None:
    import torch
    from loguru import logger

    from acestep.core.generation.handler.mlx_dit_init import MlxDitInitMixin

    original = MlxDitInitMixin._init_mlx_dit

    def init_mlx_dit(self, compile_model: bool = False) -> bool:
        ready = original(self, compile_model)
        decoder = getattr(getattr(self, "model", None), "decoder", None)
        if not ready or decoder is None:
            return ready
        parameters = sum(parameter.numel() for parameter in decoder.parameters())
        decoder.to("meta")
        gc.collect()
        if torch.backends.mps.is_available():
            torch.mps.empty_cache()
        logger.info(
            "[local-music-engine] Released the PyTorch DiT decoder ({:.2f}B parameters); "
            "MLX runs diffusion and the PyTorch fallback is disabled.",
            parameters / 1e9,
        )
        return ready

    MlxDitInitMixin._init_mlx_dit = init_mlx_dit


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
    seed_lm_sampler_from_request()
    release_torch_decoder_after_mlx_init()
    from acestep.api_server import main as ace_main

    ace_main()


if __name__ == "__main__":
    main()
