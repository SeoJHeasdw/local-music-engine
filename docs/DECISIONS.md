# 모델·라이선스 결정

최신 확인일: 2026-10-19. 코드 저장소의 라이선스와 가중치·데이터·생성 결과물의 조건은 같은 것으로 취급하지
않는다. 가중치와 생성 음원은 Git이나 코드 배포물에 넣지 않는다. 사용자가 넣는 가사와 참조 오디오의 권리는
사용자가 확인한다.

## 엔진

| 결정 | 근거 |
| --- | --- |
| 앱과 CLI의 기본 엔진은 ACE-Step 1.5 XL turbo + 4B 5Hz LM, native MLX (앱 2026-10-06, CLI 2026-10-13) | 사용자 블라인드 청취에서 3분 곡의 끊김이 적고 완성도가 높았다. turbo는 더 멜로디컬했지만 렉이 있었다 |
| `acestep-v15-xl-sft`는 쓰지 않는다 | 보컬은 나오지만 청취에서 음질이 깨졌다 |
| LM 온도 0.6, LM은 4B | 같은 6 seed에서 0.85 → 0.6이 한국어 CER 0.35 → 0.19, 영어 WER 0.45 → 0.32. 1.7B는 4B보다 나빴다 |
| MiniMax Music 3는 기본에서 제외, 모델 삭제 | 사용자가 들은 결과가 모두 반주가 비거나 90·00년대처럼 들렸다. 코드는 남기고 `--engine minimax-music3`로만 고른다 |
| BGM은 이 앱에서 만들고, 효과음은 넣지 않는다 | BGM은 같은 ACE 경로의 연주곡이다. 효과음은 모델·작업 형태·검사·화면이 모두 달라, 만든다면 에셋 엔진 쪽이 맞다 |
| BGM 반복용은 듣기용 사본에서, 마디 첫 박 킥에 맞춰 자른다 | 앱이 재생하고 내보내는 파일이다. 템포 추정(90.11)이 아니라 킥(90.0002)으로 재야 들어 본 24마디 구간이 나온다 |
| 이음새는 닮은 소리일 때 20 ms를 같은 비율로 섞는다 | 소리 크기를 지키는 섞기가 limiter 천장 위로 피크를 냈다(1.0 > 0.881) |
| BGM 문구는 "no drum fills or stops"를 담는다 | 고른 킥 연타·끊김이 있는 마디가 반복용 안에서 망가진 음악처럼 들렸다. 같은 설정의 새 두 곡에는 없었고 사용자도 그렇게 들었다 |

## 고정 판본

| 대상 | 판본 | 라이선스·출처 |
| --- | --- | --- |
| ACE-Step 코드 | `v0.1.8`, commit `dce621408bee8c31b4fcf4811682eb9359e1bc94` (`bootstrap_ace.sh`가 확인) | MIT, <https://github.com/ACE-Step/ACE-Step-1.5> |
| XL turbo DiT | `ACE-Step/acestep-v15-xl-turbo`, HF revision `d4a0b288b83ebb7e25a8c0b32c573c22e134e8ee`, 4 shard 약 20GB | 모델 카드 MIT, <https://huggingface.co/ACE-Step/acestep-v15-xl-turbo> |
| 기본 묶음(turbo DiT·VAE·Qwen embedding·1.7B LM) | HF revision `19671f406d603126926c1b7e2adc169acbcade22` | 모델 카드 MIT, <https://huggingface.co/ACE-Step/Ace-Step1.5> |
| 4B 5Hz LM | `ACE-Step/acestep-5Hz-lm-4B`, HF revision `0a3ec94b557aea7d508da38b31cfe7341f6ff737`, 2 shard 약 8.4GB | 모델 카드 MIT, <https://huggingface.co/ACE-Step/acestep-5Hz-lm-4B> |
| Python | CPython `3.12.12`, 프로젝트·ACE·검사 환경을 각각의 venv로 분리 | PSF |
| Electron / TypeScript / esbuild | `44.4.3` / `7.0.2` / `0.28.2` | MIT / Apache-2.0 / MIT |
| 로컬 LLM 도우미(선택) | Ollama 기본 API 또는 OpenAI 호환 loopback 서버 | 사용자가 설치한 런타임·모델의 조건 |

세 모델 저장소의 파일별 크기와 SHA-256은 `scripts/ace-weights.json`에 있다(가중치·설정·토크나이저 41개, 약 38.5GB;
모델 코드 `*.py`는 ACE가 시작할 때 자기 checkout에서 복사하므로 제외). `bootstrap_ace.sh`는 빠진 파일만 위 revision으로
받고 모든 파일의 SHA-256을 확인한다(`scripts/ace_weights.py verify`, 약 20초). `start_ace_api.sh`는 시작마다 고정 파일의
존재·크기와 띄울 DiT·LM이 고정 목록에 있는지 확인한 뒤 `HF_HUB_OFFLINE=1`로 ACE를 켠다. ACE는 시작할 때 모델이 없으면
최신 revision을 받고 Hugging Face가 실패하면 ModelScope에서 받으므로, 파일이 빠지면 새 가중치를 받지 않고 여기서 멈춘다.
기본 묶음은 하나라도 없으면 ACE가 묶음 전체를 다시 받으려 하므로 쓰지 않는 turbo DiT와 1.7B LM도 지우지 않는다.

가중치 SHA-256:

- Turbo DiT: `3f6e0797fad420a39bd33979eb6e840e30989e34a3794e843d23b60ec6e422d7`
- VAE: `da17edb604c40deaf09e9b24974e590d1ca83a374070e5d0884cfa4bed9a99b0`
- Qwen embedding: `0437e45c94563b09e13cb7a64478fc406947a93cb34a7e05870fc8dcd48e23fd`
- 1.7B LM: `f161689da73e5ecefa28ff780d51c2d92a00f056d021d7933c779ed5c6cd7db8`
- XL turbo DiT: `e54a8413c981797312f53d512dec9944023d03f42f0740cbe86f62582cc27bdc`,
  `b45f788b6dc9f15957cfa3d9b403f25f3f64c5211ecaf18dda45b67ea6470664`,
  `a4f6bd572eab79cf5f3e4678796f904c6d6c33680a3e6ed09034d6de7cabea4d`,
  `a868b05493c73808408f58b33a903371bb2846b53c8b19bd7476991bc8837259`
- 4B LM: `ada9d0d4ff48f112de3f7b82cd4e7d57b4245932657e8b8edc9a5ded6a23b77f`,
  `6302100c3577e2f1dbf32573e9b5e6e6b1bea7af101b433c2d3d6280faa8ab68`

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
