# 자동 품질 검사

곡을 만들면 엔진이 가사 준비·검사·필요한 재시도·추천·재생본 정리를 수행한다. 판단 범위는 **파일·소리의
측정값과 받아쓴 가사의 일치**다. 멜로디·감정·음악적 취향의 평점이 아니며 모든 새 후보의 사람 청취 상태는
`unreviewed`다. 사람 청취와의 구분은 [청취 계약](QUALITY.md)에 있다.

## 기본 흐름

1. 원문과 목표 길이를 보존하고 모델에 보낼 준비본을 만든다.
2. 곡 하나를 생성하고 내려받은 원본을 파일·크기·SHA-256·PCM 기준으로 검사한다.
3. 음량·리듬·프레임별 스펙트럼 측정, 보컬 활동 추정, 원 믹스 받아쓰기와 가사 비교를 수행한다.
4. 재생성 근거가 있으면 같은 준비본에 새 seed를 쓴다. 버전마다 **첫 생성 1회 + 재시도 최대 3회**다.
   근거가 없으면 그 시점에서 끝내며, 검사가 불확실하다는 것은 근거가 아니다.
5. 기술적 결함과 가사 일치 근거로 추천하고, 원본을 보존한 정리본을 새 artifact로 만든다.

두 버전을 요청하면 각각 최대 4회 예산을 가진다. 모든 시도의 가사·스타일·음악 설정·예상 seed를 시작할 때
고정하고 재개에도 같은 예산을 쓴다. 제출했다가 실패·중단된 시도도 예산에 포함한다. `resume`은 그 작업의
계보·요청 fingerprint·실제 파일 검증을 통과한 완료 결과만 재사용하고, 새 `generate`는 과거 결과를 가져오지
않는다. 자동 추천은 사람이 고른 최종본과 평가를 바꾸지 않는다. 원본과 다른 시도는 화면의 자동 생성 이력에 남는다.

## 화면에 보이는 것

앱은 버전마다 **확인할 것**의 개수만 표시하고, 검사 탭 맨 위에 그 항목을 시각·듣기 버튼과 함께 보여 준다. 듣기를
누르면 그 구간이 선택되어 바로 구간 다시 만들기로 이어진다. 세는 것은 두 가지다.

- 이 버전에 남은 재시도 근거(없으면 가사 차이 경고)
- 관측이 아닌 경고: 박자 불안정·타격 시각 흔들림·반주 끊김·전체 신호 끊김 의심, 무음·최대 음량 구간, 요청한
  템포·조성 미반영 등

구간이 바뀔 때 생기는 음량·소리 색 변화(`loudness_shift`, `spectral_shift`)와 박자 단위 해석 차이
(`tempo_ambiguity`)는 구간이 있는 곡이면 늘 나오는 관측이라 세지 않고, 다른 측정값과 함께 '측정값 자세히'에 접어
둔다. 저장되는 `quality.status`와 CLI의 `status` 출력은 이 관측까지 포함한 묶음 상태여서, 확인할 것이 없는
버전도 `attention`일 수 있다.

## 가사와 곡 길이 준비

Unicode와 개행을 정리하고 100자를 넘는 줄은 기존 단어·문장부호 경계에서 나눈다. 단어와 순서는 바꾸지 않는다.
보컬곡에 끝 태그(`[Outro]`·`[Ending]`·`[End]`)가 없고 길이에 여유가 있으면 `[Outro]`를 더한다. 루프·갑작스러운
끝을 명시한 요청에는 더하지 않는다.

가사 밀도는 음절 수와, 전체 길이의 75%를 보컬에 쓴다는 가정으로 추정한다. 초당 4음절 초과는 검토, 6음절
초과는 높은 밀도로 기록한다. 높은 밀도이고 랩·빠른 발화·루프·hard cut 의도가 없으면 초당 6음절에 맞춰 5초
단위로 길이를 늘린다(상한 ACE 600초, Music 3 300초. CLI·앱이 처음 요청할 수 있는 길이는 300초까지). 원래 목표
길이는 프로젝트 입력에 남고 적용한 길이는 요청에 고정되며 화면에 표시한다. 음원을 단어·문장 단위로 자르거나
숨·무음을 넣지 않는다.

## 소리와 가사 검사

PCM 검사는 전체 무음, 연속 full-scale 샘플, 길이 차이, 채널별 DC offset, 파일 경계 진폭, stereo의 mono 상쇄,
RMS/peak를 기록한다. 재생성 근거는 전체에 가까운 디지털 무음, 충분한 비율의 같은 극성 full-scale 연속 샘플,
큰 길이 차이뿐이다. 나머지는 청취 경고다. FFmpeg로 integrated LUFS·LRA·true peak를 측정만 한다.

UMXHQ는 CPU에서 보컬 활동 구간·비율과 vocal/mix 에너지 비율을 추정한다. 분리 오차가 있으므로 이 값만으로
보컬이 묻혔다거나 곡이 미완결이라고 판정하지 않는다. 분리한 보컬을 다시 섞거나 키우지 않는다.

Whisper에는 기대 가사를 주지 않는다. 원 믹스를 30초 창으로 받아쓰고(`condition_on_previous_text=False`)
segment의 timestamp·log probability·no-speech probability·compression ratio를 남긴다. 한글은 CER, 영어는 WER,
전체는 순서가 일치한 단위의 비율로 비교한다. 작성된 후렴 반복은 각각 따로 센다. `[Chorus x2]`는 반복으로,
`[Chorus 2]`는 구간 이름으로 읽는다. 신뢰도가 부족하거나 환각·과도한 반복으로 확정할 수 없으면 `unknown`이다.

| 가사 재시도 기준(신뢰도 조건을 만족할 때만) | 최소 문맥 |
| --- | --- |
| 순서 일치 비율 0.65 미만 | 기대 단위 20개 이상 |
| 한국어 CER 0.65 초과 | 한글 20음절 이상 |
| 영어 WER 0.75 초과 | 영어 6단어 이상 |
| 작성된 후렴 일치 비율 0.5 미만 | 그 후렴의 기대 단위 8개 이상 |

임계값은 이 앱의 휴리스틱이며 발음의 정답 판정이 아니다. ASR 미설치·실패·낮은 신뢰도, 서버 불통·모델
불일치·추론 timeout은 다시 만들 이유가 아니다(timeout 뒤에도 서버 추론이 계속될 수 있다).

**알려진 한계**: 전주가 있는 곡에서 첫 30초 창이 "Oh, oh, …" 같은 반복 환각으로 채워져 실제로 부른 1절이
0%로 기록될 수 있다(2026-10-13 'Till the Neon Fades' 4테이크 중 3개. 노래가 시작되는 7.5초부터 잘라 다시
받아쓰면 1절이 읽혔다). 전체 신뢰도 판정은 이 경우에도 `reliable`이었다. 보컬 활동 구간으로 받아쓰기 창을
나누는 방식은 구현하지 않았다.

## 리듬·프레임 검사

`--quality auto`와 `audio`는 결과 WAV를 모델 없이 분석하며 연주곡에도 적용한다. 요청에 고정한 BPM·박자표가
비교 기준이고, 적지 않았으면 정답을 채우지 않는다.

- 전곡 반복 박동 추정, 구간별 변화와 드리프트, 박자 후보 간격의 중앙값·MAD·95백분위 편차
- 요청 BPM과 추정 주기의 차이(절반·두 배·마디 단위로 읽는 경우 포함)
- 프레임별 RMS·peak, 스펙트럼 중심, 저·중·고역 비율, spectral flux(화면용은 최대 2,400개 지점으로 줄여 저장)
- 요청 박자표로 계산한 예상 마디 길이. 실제 다운비트를 검증한 값이 아니다

템포 추정은 약한 드럼·당김음·half/double-time·의도된 템포 변화에 흔들린다. 음량·스펙트럼 변화는 후렴의
악기 추가나 브레이크일 수 있다. 그래서 리듬 경고는 **청취 확인용**(`retryEligible=false`)이고 재생성·시간
늘리기·퀀타이즈를 실행하지 않는다. 정리본은 다시 측정해 `measuredArtifactSha256`에 연결한다.

```bash
uv run music-engine analyze-rhythm path.wav --bpm 84 --time-signature 4/4   # 읽기 전용
```

## 엔진이 적용한 템포·조성·박자 확인

ACE는 실제로 조건으로 쓴 `bpm`·`keyscale`·`timesignature`를 결과와 함께 돌려준다. 요청한 값이 도중에 버려지면
text2music에서는 LM이 고른 값이, repaint·cover에서는 `N/A`가 돌아온다. 자동 검사는 요청에 적은 항목만 비교한다
(BPM은 정수로 반올림, 조성은 대소문자·공백 무시, `4/4`와 `4`는 같음). 다르면 `engine_meta_mismatch` 경고
(화면 "요청한 템포 미반영")와 `quality.engine`에 두 값을 남긴다. 같은 입력은 seed를 바꿔도 같은 값이 버려지므로
재생성 근거로 쓰지 않는다. 소리를 측정한 것이 아니라 엔진의 보고를 대조한 것이다. Music 3는 이 값을 보고하지
않아 비교하지 않는다.

## 박자·반주 구간 진단

새 품질 계획은 `diagnosticsVersion=rhythm-diagnostics-v4`를 고정한다. 네 항목을 따로 검사하고 근거가 부족하면
`unknown`으로 둔다. 과거 계획(v1–v3)은 재개해도 그때의 검사 범위를 유지하고 저장된 보고서는 다시 쓰지 않는다.

| 항목 | 보는 것 |
| --- | --- |
| `beatTiming` | 반복 박동이 충분히 관측된 구간에서 타격 간격의 편차와 구간별 변화. 빠진 박동은 타격 소리 쉼, 필인은 불확실성으로 둔다 |
| `backingContinuity` | 분리한 반주가 주변보다 급격히 줄었다 돌아오는데 보컬은 계속되는 0.2–4초 구간. 분리 근거가 없으면 판단하지 않는다 |
| `mixContinuity` | 곡 중간의 80 ms–4초 디지털 무음(모든 채널이 거의 0, 주변 대비 45 dB 이상 감소, 경계의 급격한 변화) |
| `hitTiming` | 같은 곡의 다른 반복과 비교해 타격이 연달아 빠르거나 늦은 구간. [타격 시각 검사](TIMBRE-TRACKING.md) |

모델 없는 타격 성분 추정(median HPSS)은 공격음과 지속음의 스펙트럼 근사이며 반주 끊김 판단에는 쓰지 않는다.
보컬·반주 근거는 UMXHQ의 250 ms 에너지 시계열을 원본 SHA-256에 연결해 쓴다. 선택적인 드럼 가중치
(`drums-9619578f.pth`, 35,637,796 bytes, SHA-256 `9619578f885c54737cb0234f9f9a4a679ee4f31438fd77fd1dbe02bb16c2da0a`)는
`quality_worker.py setup --drums`로만 받는다.

`analyze-rhythm --diagnostics`는 읽기 전용이다. `--separation-json`은 원본 해시와 시각별 mix 에너지가 실제 WAV와
맞을 때만 받는다. `inspect-rhythm`은 기존 후보에 새 보고서 artifact·job·revision을 더하고 기존 QC·음원·선택·
사람 평가를 덮지 않는다.

진단 범위 밖: 연주곡의 반주 누락, 4초보다 긴 끊김, 돌아오지 않는 소실, 음색·화성·멜로디 오류. 분리 오차·
스윙·필인·루바토는 원인으로 남는다.

### 구간별 의도 대조

프롬프트의 `rubato` 같은 단어만으로는 경고를 낮추지 않는다. 의도된 템포 변화나 쉼은 **검사할 음원의
SHA-256**에 연결한 계획으로 알린다.

```json
{
  "version": "rhythm-intent-v1",
  "sourceArtifactSha256": "검사할 WAV의 64자리 소문자 SHA-256",
  "sections": [
    {"startSeconds": 16, "endSeconds": 18, "name": "보컬만 남는 쉼", "expectedRest": true},
    {"startSeconds": 32, "endSeconds": 48, "timingIntent": "tempo_change"}
  ]
}
```

```bash
uv run music-engine analyze-rhythm path.wav --diagnostics --intent-json intent.json
uv run music-engine inspect-rhythm projects/my-song --candidate-id <candidate-id> --intent-json intent.json
```

- `expectedRest=true` 또는 `accompanimentExpected=false`: 측정한 반주 감소 구간 **전체**가 계획 안에 있을 때 편곡
  쉼 후보로 분류한다. 전체 신호의 디지털 무음은 `expectedRest=true`만 덮는다.
- `timingIntent`(`rubato`·`tempo_change`): 구간 전체가 계획에 포함된 템포 드리프트만 안내로 낮춘다. 계획 안의
  불규칙한 흔들림, 일부만 겹치는 계획은 경고로 남는다.
- 입력은 최대 64 KB·128개 구간이다. 저장 검사에는 정규화한 계획과 입력 파일 해시를 남겨, 나중에 파일을 바꿔도
  과거 검사의 의미가 바뀌지 않는다. 분리 근거가 없으면 계획만으로 반주 검사를 통과시키지 않는다.

계획과 맞아도 사람 청취 승인은 아니다.

### 검증된 범위

실제 기존 음악 9곡에 통제된 변형을 넣어 평가했다(2026-10-09, 조정용 5곡 / 코드를 고정한 뒤 처음 평가한 4곡).

| 통제 변형 | 조정용 | 처음 평가 |
| --- | --- | --- |
| 50–130 ms 시간 교란을 `hitTiming`이 찾음(정답과 50% 이상 겹침) | 44/60 | 18–22/48 |
| 0.1–1.5초 전체 믹스 끊김을 `mixContinuity`가 찾음 | 15/20 | 7/16 |
| 부드러운 템포 변화·원곡·음량·페이드 파일에 경고 | 0/20 | 0/16 |

곡마다 0/12에서 11/12까지 갈렸다. 생성 모델이 실제로 내는 오류에 대한 사람의 구간 정답은 없어 자연 음악의
정확도는 측정하지 않았다. 평가 방법과 재현 명령은 [타격 시각 검사](TIMBRE-TRACKING.md#다시-평가하기)에 있다.

## 원본을 보존한 재생본 정리

`finished-audio`는 별도 파일·후보·request·`audio-finish` revision으로 저장하고 원본 SHA-256, 처리 내용, 전후
측정을 연결한다. 실패하면 검증된 원본을 유지한다. 처리는 다음뿐이다.

- 채널 평균의 절대 DC offset이 0.002보다 크면 제거한다.
- peak 0.98과 true peak −1 dBTP 안에 들도록 선형 gain을 **낮춘다**. 올리지 않는다.
- 0이 아닌 급한 파일 경계에 최대 5 ms fade를 적용한다.

길이·sample rate·channel 수·PCM 폭을 유지한다. noise gate·EQ·compression·시간 늘리기·고정 LUFS 정규화는 하지
않는다. gain 감소는 이미 생긴 clipping을, fade는 끊긴 가사나 불완전한 끝을 복원하지 않는다. 정리본의 true peak를
다시 재서 −1 dBTP를 인증하는 단계는 없다.

## 구간 수정의 자동 검사

`repaint`도 기본 `--quality auto`이며 수정 WAV 전체를 **한 번** 검사하고 정리본을 만든다. 부모 버전의 고정된
가사·길이·음악 설정을 그대로 쓰고, 새 곡의 가사 준비·Outro 추가·길이 조정은 적용하지 않는다. 경고가 있어도
자동으로 다시 repaint하지 않는다. 원곡 → 수정 원본 → 정리본 계보와 `editRange`를 보존한다. 전체 검사가 편집
경계의 연결을 보증하지 않으며, 선택 범위 밖도 모델이 바꿀 수 있으므로 직접 비교해 듣는다.

## 실행 환경과 중단 처리

`--quality audio`는 가사 모델 없이 음원 검사·정리만 하고, `--quality off`는 생성과 WAV 무결성 검사만 한다.
연주곡 `[Instrumental]`은 가사 모델을 올리지 않는다.

```bash
./scripts/bootstrap_quality.sh        # 가사가 있는 곡의 첫 검사 때 자동 실행된다
uv run music-engine generate projects/my-song --seeds 101 --quality-attempts 2
uv run music-engine generate projects/my-song --seeds 102 --quality audio
```

Apple Silicon macOS·FFmpeg·uv가 필요하다. 검사 환경 `.runtime/quality/.venv`(Python 3.12.12)는 프로젝트·엔진
환경과 분리되고, 패키지는 `requirements-quality.txt`와 `scripts/quality-requirements.txt`에 버전·해시로 고정한다.
가중치(약 1.65GB)는 `.runtime/models/quality`에 두며 Whisper revision과 모든 가중치의 SHA-256을 고정한다. 분석은
offline이고 음원·가사를 네트워크로 보내지 않는다. 모델·라이선스는 [결정 기록](DECISIONS.md)에 있다.

bootstrap과 무거운 분석은 파일 잠금으로 직렬화하고, 분석이 끝나면 worker가 종료해 메모리를 반환한다. 호출한
CLI가 사라지면 250 ms 간격의 watchdog이 worker와 bootstrap 자식 프로세스를 멈춘다.

## 실제 추론으로 확인하기

모델 없는 회귀 검사는 실제 추론 검사를 대체하지 않는다. 생성과 검사 전체는 켜 둔 XL turbo 서버에서 새
30초 연주곡을 한 번 만들어 확인한다. 원본·정리본의 분석 해시와 `unreviewed` 상태를 검사한다.

```bash
./scripts/start_ace_api.sh
# 다른 터미널에서, 존재하지 않는 출력 디렉터리를 지정한다.
uv run python scripts/smoke_rhythm_quality.py --output-dir .runtime/rhythm-smoke-new
```

## 자동 검사가 하지 않는 것

멜로디·화성·실제 박자표·마디 강세·가수 음색의 일관성, 감정 표현, 자연스러운 발음, repaint 연결부는 판단하지
않는다. 자동 보컬 재믹스, 강제 음절 절단, 주관적인 장르 점수는 구현하지 않았다. ACE의 DiT Lyrics Alignment
Score는 고정한 v0.1.8 REST/MLX 경로에서 받을 수 없어 쓰지 않는다. Whisper는 speech ASR이어서 노래에서는
신뢰도가 높아도 가사를 잘못 읽을 수 있다.
