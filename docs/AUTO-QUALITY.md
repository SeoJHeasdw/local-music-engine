# 생성 버튼의 자동 품질 관리

2026-10-04 기준. 사용자는 곡을 만들면 되고, 엔진이 가사 준비·검사·필요한 재시도·추천·
재생본 정리를 수행한다. 결과의 판단 범위는 **파일·소리의 측정값과 받아쓴 가사의 일치**다.
멜로디·감정·음악적 취향의 평점이 아니며 모든 새 후보의 사람 청취 상태는 `unreviewed`다.

## 기본 흐름

1. 원문과 목표 길이를 보존하고 모델에 보낼 준비본을 만든다.
2. 곡 하나를 생성하고 다운로드한 원본을 파일·크기·SHA-256·PCM 기준으로 검사한다.
3. 음원 측정, 로컬 보컬 활동 추정, 원 믹스 받아쓰기와 가사 비교를 수행한다.
4. 재생성 근거가 있으면 같은 준비본에 새 seed를 사용한다. 버전마다 **첫 생성 1회 + 재시도
   최대 3회**, 합쳐 최대 4회다. 재시도 근거가 없으면 그 시점에서 끝낸다. 불확실한 검사
   자체는 재시도 근거로 사용하지 않는다.
5. 이 작업 안에서 기술적 결함과 가사 일치 근거를 비교해 추천한다. 원본을 보존한 정리본을
   새 artifact로 만들고 추천·검사·처리 이력을 저장한다.

두 버전을 요청하면 각각 별도의 최대 4회 예산을 가진다. 모든 시도의 가사·스타일·음악 설정·
예상 seed를 시작할 때 고정하고 재개에도 같은 예산을 사용한다. 이미 제출했다가 실패·중단된
시도도 소모한 예산에 포함한다. `resume`은 해당 작업의 계보·요청 fingerprint·실제 파일 검증을
통과한 완료 결과만 명시적으로 재사용한다. 새 `generate`는 과거 결과를 묵시적으로 가져오지 않는다.

기존에 선택한 최종본·사람 평가를 자동 추천으로 바꾸지 않는다. 작업실은 추천본을 바로 들어 볼
수 있게 하고, 원본과 다른 시도는 **자동 생성 이력**에 보존한다. **검사**에 소리·가사·처리 내용과
재시도 이유를 표시한다. 4회 안에 문제가 해소되지 않으면 남은 경고도 함께 표시한다.

## 가사와 곡 길이 준비

Unicode와 개행을 정리하고, 100자를 넘는 긴 줄은 기존 단어·문장부호 경계에서 나눈다.
가사 단어와 순서를 삭제·교체하지 않는다. 보컬곡에 `[Outro]`·`[Ending]`·`[End]` 계열 태그가
없고 입력 길이에 여유가 있으면 `[Outro]`를 추가한다.
루프·반복용·갑작스러운 끝을 명시한 요청에는 이 끝 태그를 추가하지 않는다.

가사 밀도는 한글 음절 수와 영어의 대략적인 음절 수, 전체 길이 중 보컬에 사용할 것으로
가정한 75%를 바탕으로 추정한다. 보컬 시간당 초당 4음절 초과는 검토, 6음절 초과는 높은
밀도로 기록한다. 높은 밀도이고 랩·빠른 발화·루프·hard cut 의도가 명시되지 않았다면
초당 6음절 추정에 맞춰 5초 단위로 길이를 늘린다. 기본 Music 3 상한은 300초이며 명시적인
legacy ACE 경로만 600초다. EOS에 따른 실제 길이를 기록하고 padding하지 않는다. 원래 목표 길이는
프로젝트 입력에 남고, 적용한 길이와 준비 내용은 요청에 고정된다. 화면에도 길이 변경을 표시한다.

이 수치는 이 앱의 보수적인 계획용 추정치이며 음악의 보편적인 발성 한계가 아니다. TTS처럼
한글 60ms·영어 50ms로 음절마다 파형을 자르거나 숨·무음을 삽입하지 않는다. 노래의 박자,
지속 음과 반주를 보존하기 위해 음원 자체를 단어·문장 단위로 자동 절단하지 않는다.

## 소리와 가사 검사

PCM 검사는 전체 무음, 연속 full-scale 샘플과 그 비율, 길이 차이, 채널별 DC offset,
파일 경계 진폭, stereo의 mono 상쇄와 RMS/peak 등을 기록한다. 자동 재생성 대상은
전체 near-digital-silence, 충분한 비율의 같은 극성 full-scale 연속 샘플, 큰 길이 차이다.
작은 길이 차이·DC·파일 경계·mono 상쇄는 청취 경고다. 의도된 쉼·조용한 부분·단발성 peak를
결함으로 단정하지 않는다. `technicalScore`는 측정 가능한 기술적 결함의 내부 비교용이다.

FFmpeg는 integrated LUFS, loudness range(LRA), true peak를 측정만 한다. UMXHQ는 CPU에서
20초 구간과 앞뒤 0.5초 문맥을 사용해 보컬 활동 구간·비율, vocal/mix 에너지 비율, 끝 보컬
활동을 추정한다. 분리 오차가 있으므로 이 값만으로 보컬이 묻혔다거나 곡이 미완결됐다고 판정하지
않는다. 분리한 보컬을 결과 믹스에 다시 섞거나 자동으로 키우지 않는다.

Whisper에는 기대 가사를 prompt로 주지 않는다. 원 믹스를 내부 30초 창으로 받아쓰고,
`condition_on_previous_text=False`, `task=transcribe`로 실행한다. segment의 실제 timestamp,
log probability, no-speech probability, compression ratio와 텍스트를 남긴다. 동일한 모델의
재실행이나 분리본/믹스 재검사를 독립 판독 여러 개로 세지 않는다.

한글은 공백·문장부호를 정리한 CER, 영어는 WER, 전체는 순서가 일치한 단위의 비율을 비교한다.
후렴의 각 작성된 반복을 따로 세므로 한 번 인식한 후렴이 여러 반복을 모두 충족하지 않는다.
`[Chorus x2]` 같은 반복 표시는 비교 기대값에 반영하고 `[Chorus 2]`는 구간 이름으로 취급한다.

ASR 신뢰도가 부족하거나 timestamp가 불가능하거나 환각·과도한 반복·지원하지 않는 글자와 숫자
때문에 비교를 확정할 수 없으면 `unknown`이다. 현재 가사 비교는 한국어·영어를 지원한다.
신뢰도 조건을 만족한 큰 불일치만 재시도 근거다.

| 가사 재시도 기준 | 최소 문맥 |
| --- | --- |
| 순서 일치 비율 0.65 미만 | 기대 단위 20개 이상 |
| 한국어 CER 0.65 초과 | 한글 20음절 이상 |
| 영어 WER 0.75 초과 | 영어 6단어 이상 |
| 작성된 후렴 일치 비율 0.5 미만 | 해당 후렴 기대 단위 8개 이상 |

이 임계값은 앱의 휴리스틱이다. 발음의 정답 판정이나 장르 전체에 교정한 청취 기준이 아니다.
작은 차이는 경고와 관측값으로 남긴다. ASR 미설치·실패·낮은 신뢰도·검사 도구 실패 자체는
새 곡을 만들 이유가 아니다. 별개의 명백한 PCM 결함이 있으면 그 근거는 사용할 수 있다.
서버 불통·모델 불일치·추론 timeout도 무조건 다시 제출하지 않는다. timeout 뒤 서버 추론이
계속될 수 있기 때문이다.

## 원본을 보존한 재생본 정리

`finished-audio`는 별도 파일·후보·request·`audio-finish` revision으로 저장한다. 원본 artifact,
원본 SHA-256, 처리 내용과 전후 PCM 측정을 연결한다. 후처리에 실패하면 검증된 원본을 유지한다.

기본 처리 범위는 다음뿐이다.

- 채널 평균의 절대 DC offset이 0.002보다 크면 제거한다.
- peak ceiling 0.98 안에 들도록 선형 gain을 낮춘다. gain을 올리지 않는다.
- 측정 true peak가 -1 dBTP보다 크면 -1 dBTP를 목표로 감쇠량을 더 산정한다.
- 0이 아닌 시작·끝의 급한 파일 경계에는 필요한 경우 최대 5ms fade를 적용한다.

길이·sample rate·channel 수·PCM 폭을 유지한다. 곡의 쉼을 자르거나 noise gate·EQ·
compression·시간 늘리기를 적용하지 않는다. loudnorm은 분석에만 사용하며 고정 LUFS로
맞추지 않는다. gain 감소는 이미 clipping된 파형을 복원하지 않는다. 마지막 5ms fade도
클릭 완화일 뿐 끊긴 가사·멜로디나 불완전한 곡 끝을 복원하지 않는다. 자동 파이프라인에는
정리본의 true peak를 다시 측정해 -1 dBTP를 인증하는 과정이 없다. 원본·정리본을 같은 위치에서
비교해 들을 수 있다.

## 구간 수정의 자동 검사

CLI와 앱의 `repaint`도 기본 `--quality auto`이며, 전체 수정 WAV에 PCM·받아쓰기 검사를
**한 번** 수행하고 정리본을 만든다. `--quality audio`와 `--quality off`도 사용할 수 있다.
편집 범위와 부모 버전의 고정된 가사·길이·음악 설정은 그대로 사용한다. 새 곡의 가사 준비,
Outro 추가나 길이 조정은 적용하지 않는다. 경고가 있어도 자동으로 repaint를 다시 제출하지 않는다.

원곡 → 수정 원본 → 정리본 계보와 `editRange`를 보존한다. 결과를 추천하되 기존 사람의
최종본 선택과 평가는 바꾸지 않는다. 검사를 끝내지 못하면 상태를 `unknown`으로 남기고,
정리가 실패하면 검증된 수정 원본을 유지한다. 전체 곡을 검사하는 것이 편집 경계의 박자·
음색·반주 연결을 보증하는 것은 아니다. 선택 범위 밖도 모델이 바꿀 수 있으므로 직접 비교해 듣는다.

## 실행 환경과 중단 처리

기본 CLI는 `generate --quality auto --quality-attempts 4`다. `--quality audio`는 가사 모델 없이
음원 검사·정리를 하고, `--quality off`는 기존 생성과 WAV 무결성 검사만 한다.
연주곡 `[Instrumental]`도 가사 모델을 설치·로드하지 않고 FFmpeg/PCM만 검사한다.

```bash
./scripts/bootstrap_quality.sh
uv run music-engine generate projects/my-song --seeds 101 --quality-attempts 2
uv run music-engine generate projects/my-song --seeds 102 --quality audio
```

가사가 있는 곡의 첫 자동 검사 때 bootstrap을 자동 실행한다. Apple Silicon macOS와 FFmpeg,
uv가 필요하다. Python **3.12.12**의 `.runtime/quality/.venv`는 루트 `.venv`, Music 3의
`.runtime/minimax-music3/.venv`, 과거 ACE의
`.runtime/ace-step-1.5/.venv`와 분리된다. 버전과 해시를 고정한 패키지 목록은
`requirements-quality.txt`와 `scripts/quality-requirements.txt`다.

모델 가중치는 약 **1.65GB**이며 패키지 용량은 별도다. 모델은 `.runtime/models/quality`,
캐시는 `.runtime/quality`만 사용한다. 음원·가사를 네트워크로 보내지 않으며 분석은 offline이다.
가중치 설치만 모델 게시 서버에 접속한다. Whisper revision과 모든 가중치의 SHA-256을 고정한다.
기존 TTS 프로젝트의 환경·모델·개인 데이터를 사용하지 않는다.

bootstrap과 무거운 분석은 각각 안정된 파일 잠금으로 직렬화한다. 두 번째 분석은 모델을
올리지 않고 기다린다. 작업 종료 시 별도 worker가 종료돼 모델 메모리를 반환한다. 관리된
호출은 원 CLI가 사라지면 250ms 간격으로 확인하는 watchdog이 worker와 bootstrap 소유 child를
멈춘다. 사용자가 별도로 실행한 worker에는 이 부모 감시를 강제하지 않는다.

## 실제 검증

모델 설치 후 이 저장소의 **기존** 30초 음원 두 개를 새 도구로 분석했다. UMXHQ CPU와
MLX Whisper, FFmpeg가 모두 실행됐고 원본 SHA-256·크기는 기존 정본과 일치했다.
`shine-on-me` 30초 음원 분석은 3.83초, 최대 RSS 2.39GiB, peak memory footprint 3.30GiB였다.
이 검사는 새 음악 생성이나 정확한 발음의 청취 검증이 아니다. 기록은
`.runtime/quality/validation/`에 보존한다.

2026-10-04에는 **새 ACE 추론**을 별도로 수행해 기본 자동 생성 전체를 검증했다.
`acestep-v15-turbo` + `acestep-5Hz-lm-4B` native MLX로 30초 한글 곡을 만들었다.

| 새 추론 확인 항목 | 실제 결과 |
| --- | --- |
| 생성·자동 검사·추천·정리 | 87.706초, 최초 1회에서 종료 |
| 자동 상태 | `passed`, 가사 비교 `pass` |
| 한국어 CER / 순서 일치 비율 | 0.0714 / 0.9524 — ASR 텍스트 비교값 |
| 명시적 재개 | 0.015초, 같은 계보의 검증된 완료 결과 재사용, 새 추론 0회 |
| 원본·정리본·내보내기 | 기록된 모든 artifact 크기·SHA-256 검증 성공 |
| 사람 평가·선택 | `unreviewed`, `selectedCandidateId=null` 유지 |

리포트는 `.runtime/auto-quality-20261004-001/report.json`이다. 이 시간은 모델 설치를 포함한
최초 사용자 준비 시간이나 여러 곡의 속도 평균이 아니다. 이번 1개 짧은 곡의 자동 통과로
한국어 전반·영어 혼합곡·모든 장르의 정확도나 청취 품질 향상을 보증하지 않는다.
회귀 테스트의 가짜 client·ASR 결과는 실제 추론으로 기록하지 않는다.

같은 실제 생성 결과를 원본으로 **새 repaint 추론**과 자동 검사를 추가 수행했다.

| 실제 구간 수정 확인 항목 | 결과 |
| --- | --- |
| repaint·검사·정리 | 37.179초, repaint 제출 1회 |
| 자동 상태 | `attention`, 가사 비교 `warning` |
| 한국어 CER / 순서 일치 / 후렴 일치 | 0.2143 / 0.8095 / 0.70 |
| 추가 추론 | 없음, `retryReasons=[]`; repaint는 자동 재추론하지 않음 |
| 원본과 모든 artifact | 원본 불변, 크기·SHA-256 검증 성공 |
| 정리본의 별도 FFmpeg 실측 | -12.84 LUFS / LRA 6.0 / -1.0 dBTP |
| 사람 평가 | `unreviewed` |

리포트는 `.runtime/auto-quality-20261004-001/repaint-report.json`이다. 후렴의 받아쓰기
일치 비율이 낮아 경고를 남겼으며, 실제 가사 누락인지 인식 오류인지는 청취해야 한다.
원본 곡은 자동 통과했지만 이 수정본은 확인할 부분이 남았다. 수정했다는 이유로 더 좋은
음원이라고 표시하지 않는다. 표의 정리본 FFmpeg 측정은 이번 검증에서 별도로 실행한 것이다.

## 연구 근거·라이선스·남는 범위

ACE의 공식 가이드는 간결한 구조 태그와 Caption/Lyrics의 일관성, 후보 생성 후 점수 비교를
설명한다. 현재 엔진은 turbo에 8 steps, SFT/base에 50 steps를 사용한다. 단계 수를 무조건
올리거나 사용자가 쓰지 않은 조성·BPM을 임의로 채우지 않는다.
[ACE 공식 Tutorial](https://github.com/ace-step/ACE-Step-1.5/blob/main/docs/en/Tutorial.md)

Whisper는 speech ASR이며 언어별 편차·환각·반복이 있다. 노래에서는 ASR 신뢰도가 높아도
가사를 잘못 읽을 수 있으므로 결과를 청취 승인으로 바꾸지 않는다.
[Whisper 공식 model card](https://github.com/openai/whisper/blob/main/model-card.md)

2025 ICME 논문은 보컬 분리본의 전곡 받아쓰기가 항상 낫지 않으며, 분리본으로 보컬 활동 경계를
찾아 원 믹스의 구간을 읽는 방식의 이점을 보고한다. 현재 구현은 분리본을 관측에 쓰고 원 믹스를
Whisper 내부 창으로 읽는 첫 단계다. 논문의 vocal-activity 기반 ASR 분할은 구현하지 않았으며
해당 논문의 평가 언어에 한국어도 없다.
[연구 논문](https://arxiv.org/abs/2506.15514)

| 사용 구성 | 라이선스·원 출처 |
| --- | --- |
| MLX Whisper 구현 | Apple MIT: [공식 구현](https://github.com/ml-explore/mlx-examples/tree/main/whisper), [라이선스](https://github.com/ml-explore/mlx-examples/blob/main/LICENSE) |
| Whisper 원 코드·가중치 | MIT: [OpenAI Whisper](https://github.com/openai/whisper) |
| MLX 변환 large-v3-turbo | 원 Whisper의 MLX 변환: [게시 모델](https://huggingface.co/mlx-community/whisper-large-v3-turbo), revision `a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb` |
| UMXHQ 보컬 코드·가중치 | MIT: [Open-Unmix 코드](https://github.com/sigsep/open-unmix-pytorch), [저자 게시 UMXHQ 가중치](https://zenodo.org/records/3370489); Zenodo metadata의 license `mit-license` 확인 |
| FFmpeg | 빌드 옵션에 따라 LGPL/GPL: [공식 라이선스](https://ffmpeg.org/legal.html), [loudnorm 측정 설명](https://ffmpeg.org/ffmpeg-filters.html#loudnorm) |

Open-Unmix의 기본 `umxl`은 가중치가 CC BY-NC-SA이므로 명시적으로 `umxhq`만 쓴다.
Demucs는 코드 MIT와 pretrained 가중치 허용 범위가 달라 기본에 넣지 않았다.
[Demucs 저자의 가중치 안내](https://github.com/facebookresearch/demucs/issues/327#issuecomment-1134828611)
Spleeter는 공식 현재 패키지의 Python `<3.12` 조건 때문에 사용하지 않는다.
[Spleeter 공식 의존성](https://raw.githubusercontent.com/deezer/spleeter/master/pyproject.toml)

멜로디·화성·박자·가수 음색의 일관성, 감정 표현, 자연스러운 발음, repaint 연결부는 이 검사가
해결했다고 주장하지 않는다. 자동 보컬 재믹스·강제 음절 절단·주관적인 장르 점수는 구현하지
않았다. ACE의 DiT Lyrics Alignment Score는 현재 pinned v0.1.8 REST/MLX 경로에서 사용할 수
있는 응답으로 연결돼 있지 않아 자동 기준에 쓰지 않는다. 구조·태그 준비와 측정 가능한 결함의
선별은 자동화하되, 추천 결과의 표현과 음악성은 사람이 듣고 평가한다.
