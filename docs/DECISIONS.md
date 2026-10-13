# 모델·라이선스 결정

최신 확인일: 2026-10-13. 코드 저장소의 라이선스와 가중치·데이터·생성 결과물의 조건은 같은 것으로 취급하지
않는다. 가중치와 생성 음원은 Git이나 코드 배포물에 넣지 않는다. 사용자가 넣는 가사와 참조 오디오의 권리는
사용자가 확인한다.

## 엔진

| 결정 | 근거 |
| --- | --- |
| 앱과 CLI의 기본 엔진은 ACE-Step 1.5 XL turbo + 4B 5Hz LM, native MLX (앱 2026-10-06, CLI 2026-10-13) | 사용자 블라인드 청취에서 3분 곡의 끊김이 적고 완성도가 높았다. turbo는 더 멜로디컬했지만 렉이 있었다 |
| `acestep-v15-xl-sft`는 쓰지 않는다 | 보컬은 나오지만 청취에서 음질이 깨졌다 |
| LM 온도 0.6, LM은 4B | 같은 6 seed에서 0.85 → 0.6이 한국어 CER 0.35 → 0.19, 영어 WER 0.45 → 0.32. 1.7B는 4B보다 나빴다 |
| MiniMax Music 3는 기본에서 제외, 모델 삭제 | 사용자가 들은 결과가 모두 반주가 비거나 90·00년대처럼 들렸다. 코드는 남기고 `--engine minimax-music3`로만 고른다 |

## 고정 판본

| 대상 | 판본 | 라이선스·출처 |
| --- | --- | --- |
| ACE-Step 코드 | `v0.1.8`, commit `dce621408bee8c31b4fcf4811682eb9359e1bc94` (`bootstrap_ace.sh`가 확인) | MIT, <https://github.com/ACE-Step/ACE-Step-1.5> |
| XL turbo DiT | `ACE-Step/acestep-v15-xl-turbo`, HF revision `d4a0b288b83ebb7e25a8c0b32c573c22e134e8ee`, 4 shard 약 20GB | 모델 카드 MIT |
| 기본 묶음(turbo DiT·VAE·Qwen embedding·1.7B LM) | HF revision `19671f406d603126926c1b7e2adc169acbcade22` | 모델 카드 MIT, <https://huggingface.co/ACE-Step/Ace-Step1.5> |
| 4B 5Hz LM | `acestep-5Hz-lm-4B`, 약 8GB. revision과 라이선스 표기는 이 문서에 기록된 적이 없다(확인 필요) | ACE-Step 게시 모델 |
| Python | CPython `3.12.12`, 프로젝트·ACE·검사 환경을 각각의 venv로 분리 | PSF |
| Electron / TypeScript / esbuild | `44.4.3` / `7.0.2` / `0.28.2` | MIT / Apache-2.0 / MIT |
| 로컬 LLM 도우미(선택) | Ollama 기본 API 또는 OpenAI 호환 loopback 서버 | 사용자가 설치한 런타임·모델의 조건 |

ACE는 시작할 때 기본 묶음이 하나라도 없으면 묶음 전체를 다시 받는다. 그래서 쓰지 않는 turbo DiT와 1.7B LM도
지우지 않는다. 기본 묶음 가중치의 SHA-256:

- Turbo DiT: `3f6e0797fad420a39bd33979eb6e840e30989e34a3794e843d23b60ec6e422d7`
- VAE: `da17edb604c40deaf09e9b24974e590d1ca83a374070e5d0884cfa4bed9a99b0`
- Qwen embedding: `0437e45c94563b09e13cb7a64478fc406947a93cb34a7e05870fc8dcd48e23fd`
- 1.7B LM: `f161689da73e5ecefa28ff780d51c2d92a00f056d021d7933c779ed5c6cd7db8`

## 검사 도구

| 구성 | 라이선스·출처 |
| --- | --- |
| MLX Whisper 구현 | Apple MIT: [구현](https://github.com/ml-explore/mlx-examples/tree/main/whisper), [라이선스](https://github.com/ml-explore/mlx-examples/blob/main/LICENSE) |
| Whisper 원 코드·가중치 | MIT: [OpenAI Whisper](https://github.com/openai/whisper) |
| MLX 변환 large-v3-turbo | [게시 모델](https://huggingface.co/mlx-community/whisper-large-v3-turbo), revision `a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb` |
| UMXHQ 보컬·드럼 코드와 가중치 | MIT: [Open-Unmix](https://github.com/sigsep/open-unmix-pytorch), [저자 게시 가중치](https://zenodo.org/records/3370489) |
| FFmpeg | 빌드 옵션에 따라 LGPL/GPL: [라이선스](https://ffmpeg.org/legal.html) |

채택하지 않은 것: Open-Unmix의 기본 `umxl`(가중치가 CC BY-NC-SA), Demucs(코드는 MIT지만
[pretrained 가중치의 허용 범위](https://github.com/facebookresearch/demucs/issues/327#issuecomment-1134828611)가
다름), Spleeter(Python `<3.12` 조건), SongEval(README와 LICENSE의 표기 범위가 불명확).

## Music 3 (이름으로 고를 때만)

`scripts/bootstrap_music3.sh`가 다시 받는 판본은 `mlx-community/MiniMax-Music3-bf16` revision
`83a5f2d365673689df5c8f36e21e108751fd92ea`(6 shard SHA-256 고정)와 `mlx-audio` commit
`784b29e2691a93ca7483147d86f61859dfaa6296`이다. 가중치는 MiniMax-Music3 Community License를 따른다:
[원 라이선스](https://huggingface.co/MiniMaxAI/MiniMax-Music3/blob/main/LICENSE)의 상업 제품·서비스 UI 표기,
일정 매출 이상의 별도 허가, 공개 콘텐츠의 AI 생성 고지 조건이 있다. 옮겨 쓴 코드의 고지는
[THIRD-PARTY-NOTICES](../THIRD-PARTY-NOTICES.md)에 있다.

## 보류

- FastAPI 서버: 앱은 CLI를 자식 프로세스로 실행하고 `project.json`으로 진행을 읽는다. 여러 클라이언트가 동시에
  붙어야 할 때 loopback 인증과 함께 추가한다.
