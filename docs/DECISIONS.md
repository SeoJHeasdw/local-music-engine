# 결정 기록

확인일: 2026-09-22

## 채택

| 대상 | 고정 판본/확인값 | 라이선스와 출처 | 결정 |
| --- | --- | --- | --- |
| ACE-Step 코드 | `v0.1.8`, commit `dce621408bee8c31b4fcf4811682eb9359e1bc94` | 저장소 `LICENSE`의 MIT, <https://github.com/ACE-Step/ACE-Step-1.5> | 공식 macOS MLX REST 경로 사용 |
| 통합 모델 묶음 | HF revision `19671f406d603126926c1b7e2adc169acbcade22` | 모델 카드 MIT, <https://huggingface.co/ACE-Step/Ace-Step1.5> | Turbo DiT·VAE·Qwen embedding 사용 |
| 0.6B LM | HF revision `148d8ea0225bdab342ee1ae3a354275ccd60ca80` | 모델 카드 MIT, <https://huggingface.co/ACE-Step/acestep-5Hz-lm-0.6B> | 36GB Mac의 초기 탐색 LM |
| Python | CPython `3.12.12` | PSF 라이선스 | 제품과 ACE 환경을 별도 venv로 고정 |
| Electron | `44.4.3` | MIT, <https://github.com/electron/electron> | 후보 비교 desktop shell |
| TypeScript / esbuild | `7.0.2` / `0.28.2` | Apache-2.0 / MIT | 타입 검사와 세 진입점 번들 |
| 로컬 LLM 도우미 (선택) | Ollama 기본 API 또는 OpenAI 호환 loopback 서버. 실측 모델 `qwen3.6:27b` | 사용자가 설치한 런타임·모델의 조건을 따른다 | 의존성이 아닌 선택 기능. 없으면 규칙 도우미와 엔진 초안으로 동작 |

통합 묶음은 1.7B LM도 포함한다. 0.6B를 초기 선택해도 공식 main snapshot 때문에 디스크에는
1.7B가 함께 내려왔지만 서버는 0.6B를 로드한다. 실측 모델 폴더는 약 11GB다.

가중치 SHA-256:

- Turbo DiT: `3f6e0797fad420a39bd33979eb6e840e30989e34a3794e843d23b60ec6e422d7`
- VAE: `da17edb604c40deaf09e9b24974e590d1ca83a374070e5d0884cfa4bed9a99b0`
- Qwen embedding: `0437e45c94563b09e13cb7a64478fc406947a93cb34a7e05870fc8dcd48e23fd`
- 번들 1.7B LM: `f161689da73e5ecefa28ff780d51c2d92a00f056d021d7933c779ed5c6cd7db8`
- 선택 0.6B LM: `5d92a60806e2e88c04de58ddc6dde93f2bc8f1336162b3ad5853886c9bcc6b82`

## 보류

- XL/SFT 비교: Turbo의 실제 한국어 청취가 끝난 뒤 별도 다운로드 용량과 실험 수를 정한다.
- 한국어 ASR: Apple `mlx-whisper` 코드는 MIT이며 word timestamp를 지원한다. OpenAI
  `whisper-large-v3-turbo` 공식 모델 카드는 MIT, 원본 weight는 약 1.62GB다. 다만 노래 mix를
  직접 읽는 오탐 기준을 정하지 못했으므로 분리기와 함께 실제 검증하기 전에는 설치하지 않는다.
- 보컬 분리: Demucs 코드는 MIT지만 공식 저장소가 2025년에 archive됐고 pretrained weight와
  훈련 데이터의 사용 조건에 대한 upstream 답변이 명확하지 않다. weight 조건을 특정하지 못한
  상태에서 기본 의존성으로 채택하지 않는다.
- SongEval: README와 LICENSE 표기 범위가 명확해질 때까지 필수 평가기로 쓰지 않는다.
- FastAPI: 앱은 CLI를 자식 프로세스로 실행하고 `project.json`으로 진행을 읽는다. 여러 클라이언트가 동시에 붙어야 할 때 loopback 인증과 함께 추가한다.

코드 저장소 라이선스와 가중치·데이터·생성 결과물의 조건은 같은 것으로 취급하지 않는다.
사용자가 넣는 가사와 참조 오디오의 권리는 사용자가 확인해야 한다.
