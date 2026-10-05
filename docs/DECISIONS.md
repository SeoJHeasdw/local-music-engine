# 결정 기록

최신 확인일: 2026-10-06. 아래 과거 선택 기록은 당시의 결정으로 보존한다.

## 2026-10-06 공식 정밀도와 caption v4

| 대상 | 고정 판본 | 출처·조건 | 선택 |
| --- | --- | --- | --- |
| Music 3 가중치 | HF `mlx-community/MiniMax-Music3-bf16` revision `83a5f2d365673689df5c8f36e21e108751fd92ea`, 6개 shard SHA-256 고정(`MODEL_SHARDS`) | MiniMax-Music3 Community License | 기본 엔진. 기존 MXFP8 판본을 대체 |
| 정밀도 기준 | SGLang-Omni commit `531ac8a2ac7ededc9f8e3616c170e4705109eacc`의 `sglang_omni/models/minimax_music3` | Apache-2.0 | AR(global·local LM) bfloat16, condition encoder·DiT·vocoder float32 |
| caption 형식 | MiniMax-AI/MiniMax-Music3 commit `945655064d59b98004dd70002e7eb5c8c6e11373`의 `skills/music-caption-rewriter` | 저장소에 별도 라이선스 표기 없음 | 템플릿 문장은 복사하지 않고 13개 항목 형식만 따른다 |
| 음악성 보조 지표(실험) | `audiobox_aesthetics` | CC-BY 4.0, Meta | scratch 환경에서만 사용. 프로젝트 의존성·자동 판정에 넣지 않음 |

기준 서버와 우리 MLX 이식판의 프롬프트 조립·무조건부 입력·CFG(AR 1.5, top-k 50, DiT 1.7)·온도 1.0·30 step은
같았고 정밀도만 달랐다. MXFP8 판본은 8B 구조 모델과 DiT까지 8비트로 계산했다. CFG는 조건부·무조건부
logit 차이를 키우므로 양자화 오차도 함께 커진다. 공식 정밀도로 만든 감성 힙합 60초를 사용자가 처음으로
"이제야 노래 같다"고 평가했다(caption·길이도 함께 바뀌어 단일 원인은 아니다). 전체 BF16+FP32를 한꺼번에
올리면 36GB Mac의 25.5GB 한도를 넘어, 곡마다 구조 단계와 음색 단계를 차례로 적재한다. MXFP8 대비 60초
생성은 약 8.5분에서 약 10분이 됐다. 같은 날 만든 MXFP8 + BF16 depth·DiT 부분 교체(overlay)는 이 결정으로
대체해 코드에서 제거했다. 기존 `.runtime/models/minimax-music3-mxfp8`와 overlay 파일은 삭제하지 않았다.

## 2026-10-05 기본 엔진 전환

사용자가 MiniMax Music 3로 완전한 전환을 요청했다. 기본 생성·앱 실행·초안과 설정은 Music 3로
옮기며, 과거 ACE 요청·음원과 환경은 삭제하지 않는다. 과거 코드 경로는 명시적인 legacy 선택에서만 쓴다.

| 대상 | 고정 판본 | 출처·조건 | 선택 |
| --- | --- | --- | --- |
| MLX 실행 구현 | `mlx-audio` commit `784b29e2691a93ca7483147d86f61859dfaa6296`, package 0.4.8 | [MLX 구현](https://github.com/Blaizzy/mlx-audio/tree/784b29e2691a93ca7483147d86f61859dfaa6296), 커뮤니티 포트 | 독립 Python 3.12.12 환경, PyTorch 없이 native MLX |
| Music 3 MXFP8 가중치 | HF revision `d00a12c3c7f80eb66379dd02dd0f30ed0ce2d96e` | [변환 모델](https://huggingface.co/mlx-community/MiniMax-Music3-mxfp8), MiniMax-Music3 Community License | 약 13GB, 가사 전달을 중시하는 양자화 판본 |
| MLX | 0.31.2 | [MLX](https://github.com/ml-explore/mlx), MIT | Apple M4 Max 36GiB에서 실제 실행 확인 대상 |
| 로컬 task 서버 | 프로젝트 `scripts/music3_api_server.py` | loopback·Bearer 인증, JSON 생성·opaque 완료 음원 다운로드만 제공 | 모델과 생성 worker를 하나씩 소유 |

MiniMax의 공개 Music 3 가중치는 Apache/MIT로 일괄 취급하지 않는다. [원 라이선스](https://huggingface.co/MiniMaxAI/MiniMax-Music3/blob/main/LICENSE)의
상업 제품·서비스 UI 표기, 일정 매출 이상의 별도 허가, 공개 콘텐츠의 AI 생성 고지 등의 조건을
따른다. 프로젝트 코드는 MIT이며, 코드·모델·생성 결과물의 조건은 서로 구분한다.
가중치와 생성 음원은 Git이나 코드 배포물에 포함하지 않는다.

현재 채택한 MLX 구현은 text2music이다. 음원 참조·커버·구간 repaint는 지원하지 않는다.
기존 제작 규칙과 곡 계획은 구조화 caption으로 옮기고, 원문·과거 요청·실제 전송본을 구분한다.
300초는 앱의 요청 상한이며 실제 EOS에 따라 더 짧게 끝날 수 있다. 가중치 크기와 실행 peak
메모리를 혼동하지 않으며 실제 추론 결과는 [HANDOFF](HANDOFF.md)에 별도 기록한다.

## 2026-09-22 초기 ACE 선택 기록

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
