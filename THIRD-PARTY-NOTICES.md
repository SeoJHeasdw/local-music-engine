# Third-party notices

The memory-bounded autoregressive loop and acoustic chunk execution in
`scripts/music3_api_server.py` adapt the MiniMax Music 3 implementation in
[Blaizzy/mlx-audio](https://github.com/Blaizzy/mlx-audio/tree/784b29e2691a93ca7483147d86f61859dfaa6296),
specifically `mlx_audio/music/models/minimax_music3/ar.py` and
`mlx_audio/music/models/minimax_music3/minimax_music3.py`.

The staged precision (AR in bfloat16, acoustic stage in float32) follows the
reference [SGLang-Omni](https://github.com/sgl-project/sglang-omni) MiniMax Music 3
pipeline (Apache-2.0); no SGLang-Omni code is copied. Weights are the
[mlx-community/MiniMax-Music3-bf16](https://huggingface.co/mlx-community/MiniMax-Music3-bf16)
conversion at revision `83a5f2d365673689df5c8f36e21e108751fd92ea`, governed by the
MiniMax-Music3 Community License.

The caption field layout follows the published
[music-caption-rewriter](https://github.com/MiniMax-AI/MiniMax-Music3/tree/main/skills/music-caption-rewriter)
schema. No template text is copied into this repository.

The upstream source license is reproduced below. Model weights are downloaded
separately and remain governed by the MiniMax-Music3 Community License; the source
license does not grant additional rights to those weights.

MIT License

Copyright (c) 2024 Prince Canuma

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
