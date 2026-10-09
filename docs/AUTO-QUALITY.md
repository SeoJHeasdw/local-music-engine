# 생성 버튼의 자동 품질 관리

2026-10-07 기준. 사용자는 곡을 만들면 되고, 엔진이 가사 준비·검사·필요한 재시도·추천·
재생본 정리를 수행한다. 결과의 판단 범위는 **파일·소리의 측정값과 받아쓴 가사의 일치**다.
멜로디·감정·음악적 취향의 평점이 아니며 모든 새 후보의 사람 청취 상태는 `unreviewed`다.

## 기본 흐름

1. 원문과 목표 길이를 보존하고 모델에 보낼 준비본을 만든다.
2. 곡 하나를 생성하고 다운로드한 원본을 파일·크기·SHA-256·PCM 기준으로 검사한다.
3. 음원·리듬·프레임별 음량과 스펙트럼 측정, 로컬 보컬 활동 추정, 원 믹스 받아쓰기와 가사 비교를 수행한다.
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

## 생성 흐름에 포함한 리듬·프레임 검사

`--quality auto`와 `--quality audio`는 결과 WAV를 받은 뒤 모델 없이 리듬·음량·스펙트럼을
자동 분석한다. 별도의 품질 모델 설치가 필요하지 않으며 연주곡에도 적용한다. 생성 전에 고정한
요청의 BPM·박자표를 비교 기준으로 사용하므로, 생성 뒤 프로젝트 설정을 바꿔도 과거 후보의
기준은 바뀌지 않는다. BPM·박자표를 쓰지 않았다면 임의로 정답을 채우지 않는다.

다음 관측값을 후보의 자동 검사에 남긴다.

- 전곡 반복 박동 추정, 구간별 변화와 드리프트, 소리에서 관측한 박자 후보 시각.
- 박자 후보 간격의 중앙값·MAD·95백분위 시간 편차. 빠진 박동 후보는 추정 주기의 배수로 구분한다.
- 요청 BPM과 추정 반복 주기의 차이. 절반·두 배, 복합박·마디 단위로 읽는 경우도 함께 기록한다.
- 프레임별 RMS dBFS·peak, 스펙트럼 중심 주파수, 저·중·고역 에너지 비율과 spectral flux.
- 요청 박자표에 따른 예상 마디 길이. 실제 다운비트·박자표를 검증한 결과는 아니다.

요청 BPM은 4분음표 기준이며 `6/8` 한 마디는 4분음표 3개 길이로 투영한다. 요청 BPM이 없으면
추정 반복 주기를 4분음표라고 가정해 마디 길이를 만들지 않는다. 관측한 박자 후보나 예상
마디 길이를 실제 소리의 정확한 마디 경계로 단정하지 않는다. 화면용 프레임 시계열은 최대 2,400개
지점으로 줄여 저장하고, 원 분석의 프레임 길이·hop과 표시용 집계 간격을 구분해 기록한다.
표시 지점 수를 줄이는 것은 분석 자체의 시간 해상도를 바꾸는 처리가 아니다.
추정 신뢰도는 onset 반복의 강도이며 실제 음악의 4분음표를 맞혔을 확률이 아니다.
재생본 정리 후에는 해당 파일을 다시 측정하고 `measuredArtifactSha256`에 실제 파일 해시를 연결한다.
`rhythmVersion` 없는 과거 품질 계획은 명시적 재개 때 새 검사를 묵시적으로 추가하지 않는다.

템포 추정은 약한 드럼·당김음·half-time·double-time 해석·의도된 템포 변화에 영향을 받는다.
음량이나 스펙트럼 변화도 후렴의 악기 추가·브레이크·필인일 수 있다. 현재 리듬 경고는
**청취 확인용**이며 `retryEligible=false`다. 이 경고만으로 재생성·시간 늘리기·퀀타이즈를
실행하지 않는다. 자동 분석은 원본을 변경하지 않으며 사람 청취 상태는 `unreviewed`다.

기존 WAV를 읽기 전용으로 분석할 수도 있다. JSON 한 개를 출력하고 프로젝트·음원을 수정하지 않는다.

```bash
uv run music-engine analyze-rhythm path.wav --bpm 84 --time-signature 4/4
```

## 박자·반주 구간 진단과 기존 후보 검사

새 품질 계획에는 `diagnosticsVersion=rhythm-diagnostics-v4`를 고정한다. 타격 성분의 박자 간격,
분리한 보컬·반주의 연속성, 전체 신호의 디지털 끊김, 반복과 비교한 타격 시각을 별도 항목으로 검사하고,
근거가 부족한 항목은 `unknown`으로 둔다. 기존 계획에 이 버전이 없으면 재개 때 추가 진단을 묵시적으로
실행하지 않는다. 이미 고정한 v1·v2·v3 계획은 해당 버전의 검사 범위를 유지하고, 완료한 과거 보고서는
다시 쓰지 않는다.

- 타격 성분은 모델 없이 median HPSS로 추정할 수 있다. 이는 음의 공격음과 지속음을 구분하는
  스펙트럼 근사이며 드럼·보컬·반주의 의미별 분리가 아니다. 반주 끊김 판단에는 사용할 수 없다.
- 충분히 관측한 반복 박동과 안정된 주변 구간을 비교해 시간 편차·구간별 변화를 찾는다.
  빠진 박동은 타격 소리 쉼으로, 밀도가 늘어난 필인·경쟁하는 onset은 불확실성으로 처리한다.
  전곡 추정이 약하면 서로 인접한 신뢰 가능한 구간만 분석하고 전곡 상태는 미확정으로 남긴다.
- 반주 끊김 의심은 별도로 분리한 반주가 주변보다 급격히 줄었다 돌아오며 보컬이 계속되는
  구간이다. 앞뒤 지지 구간·원본 일치 범위·보컬 활동을 모두 요구한다. 의도된 쉼·분리 누출은
  여전히 가능하며 명시적인 쉼 계획은 `possible_arrangement_break`로 기록한다.

UMXHQ가 이미 분석하는 보컬·잔여 반주에 250 ms 에너지 시계열과 원본 SHA-256을 연결한다.
선택적인 UMXHQ 드럼 가중치가 검증되어 있으면 같은 프로세스에서 드럼 onset도 측정한다.
가중치는 Zenodo 원 저자 배포 `drums-9619578f.pth`, 35,637,796 bytes,
SHA-256 `9619578f885c54737cb0234f9f9a4a679ee4f31438fd77fd1dbe02bb16c2da0a`이다.
준비는 `quality_worker.py setup --drums`로 명시적으로 하며, 분석 중 가중치를 다운로드하지 않는다.
가중치·분리 결과·로그는 Git에 넣지 않는다.

`analyze-rhythm --diagnostics`는 읽기 전용이다. `--separation-json`은 원본 해시와 시각별
mix 에너지가 실제 WAV와 맞을 때만 분리 근거를 받는다. 과거 자료에 원본 해시가 없으면
그 한계를 표시하고, 수정본으로 전달할 때는 PCM이 바이트 단위로 같은 구간만 허용한다.
`inspect-rhythm`은 새 보고서 artifact·검사 job·revision을 추가한다. 기존 QC·음원·선택·사람
평가를 덮지 않고, 화면에서 최신의 파일·크기·SHA-256이 검증된 검사만 함께 보여준다.

### 구간별 의도 대조와 검사 범위

v2는 프롬프트의 `rubato`, `tempo changes` 같은 단어만으로 전곡 박자 경고를 낮추지 않는다.
`no rubato`도 경고를 감추지 않는다. `--intent-json`으로 **현재 검사 음원의 SHA-256**과
구간별 계획을 명시하면 측정한 변화와 대조한다. 이름·장르·마디 경계만으로 경고를 제외하지 않는다.

```json
{
  "version": "rhythm-intent-v1",
  "sourceArtifactSha256": "검사할 WAV의 64자리 소문자 SHA-256으로 교체",
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

`expectedRest=true` 또는 `accompanimentExpected=false`는 **측정한 반주 감소 구간 전체**가
계획 안에 있을 때 편곡 쉼 후보로 분류한다. `timingIntent`는 `rubato` 또는 `tempo_change`이며,
구간 전체가 계획에 포함된 `tempo_drift` 관측만 안내로 낮춘다. 자세한 진단과 기본 템포 변화
관측에 같은 규칙을 적용하고 원 측정값을 남긴다. 일부만 겹치는 계획, 다른 구간의 계획,
템포 계획 안의 불규칙한 `jitter`는 경고로 남는다. 계획과 맞아도 사람 청취 승인은 아니다.

입력은 최대 64 KB·128개 구간이며 해시·범위·자료형·지원 필드를 먼저 검증한다. 저장 검사에는
정규화한 계획 스냅샷과 입력 파일 해시를 보고서·job에 남기므로 나중에 입력 파일을 바꿔도
과거 검사 의미가 바뀌지 않는다. 보컬·반주 분리 근거가 없으면 계획만으로 반주 검사를 통과시키지 않는다.

전곡 반복 추정이 약해도 인접한 두 개 이상의 지지된 구간에서 실제 onset을 후보로 연결한다.
지지되지 않은 구간을 잇거나 가상 박자를 채우지 않는다. 표시 한도 64개는 경고를 우선하며,
전체 관측 수·생략 수·생략된 경고 수를 남긴다. 표시에서 생략돼도 전체 검사 경고 상태는 유지한다.
수정본으로 전달한 분리 자료는 PCM이 일치하는 RMS 창 전체만 사용한다. 제외된 창은 미측정으로
남기고 보간·앞뒤 비교로 건너지 않으며, 멀리 떨어진 유효 구간의 관측은 계속 검사한다.

현재 진단의 범위는 관측 가능한 타격 간격과, 보컬이 계속되는 동안 앞뒤 반주가 급격히
줄었다 돌아오는 0.2–4초 구간이다. 연주곡의 반주 누락, 더 긴 끊김, 복귀 없는 소실,
음색·화성·멜로디 오류 전부를 탐지하지 않는다. 분리 오차·스윙·필인·루바토도 원인으로 남는다.
이 한계는 [박자 추적 연구](https://arxiv.org/abs/2407.21658)의 어려운 장르·연속성 한계와,
[생성 음악 평가 연구](https://arxiv.org/abs/2506.19085)의 자동 지표와 사람 선호 대조 필요성과도 맞닿는다.

코드 회귀 검사는 합성 PCM과 명시적 특징을 사용한다. 실제 음악의 정확도 수치는 아니다.
실제 성능을 평가하려면 별도 음원 집합에 사람이 `오류 / 의도된 표현 / 판단 보류`와 구간을
표시하고, 조정에 쓴 곡과 평가할 곡을 분리한 뒤 오류 탐지 precision·recall, 의도된 표현 오탐률,
판단 보류율을 함께 측정해야 한다. 모든 오류 탐지나 의도 100% 구분을 보장하지 않는다.

### v3 전체 신호 검사

v3는 원본 PCM을 최대 1초 청크로 읽고 파일 SHA-256을 전후 확인해, 곡 중간의 80 ms–4초
디지털 무음을 추가 관찰한다. 모든 채널이 거의 0이어야 하며 양쪽 0.5초 창의 활동과 주변 대비
45 dB 이상 감소, 경계 20 ms 에너지의 급격한 변화를 요구한다. 조용한 구간·페이드·시작·끝의
무음을 오류로 확정하지 않는다. `expectedRest=true`가 전체 무음 구간을 덮으면 선언한 쉼과
일치하는 관측으로 안내한다. `accompanimentExpected=false`만으로 전체 신호 끊김을 제외하지 않는다.
이 관측도 `retryEligible=false`이고 사람 청취 상태를 바꾸지 않는다.

같은 해시에 연결된 분리 자료·원본 믹스·HPSS 타격 추정이 있으면 반복되는 반주 음량 형태,
분리 추정과 원본의 불일치, 동시에 감소하는 믹스·타격 에너지를 추가 근거로 기록한다.
반복 형태만으로 의도를 확정하거나 경고를 제거하지 않는다. 정리 전 원본과 현재 파일의 해시가
다르면 두 자료를 같은 측정처럼 대조하지 않는다. 모델·새 추론은 이 과정에 필요하지 않다.

### v4 타격 시각 검사

v4는 v3에 `hitTiming` 항목을 더한다. 측정한 타격을 같은 곡의 앞뒤 반복에 있는 같은 타격과 비교하고,
여러 타격이 연달아 빠르거나 늦은 구간을 `repeated_hit_timing_shift_suspected` 경고로 남긴다.
부드러운 템포 변화는 `smooth_timing_change_observed` 정보로 따로 관찰하며 경고하지 않는다.
반복되는 리듬이 부족하면 `unknown`이다. 모델·요청 BPM·박자 격자를 쓰지 않는다.
이 관측도 `retryEligible=false`이고 추천 순위와 사람 청취 상태를 바꾸지 않는다.
방법·결과 읽는 법·한계는 [반복 대조 타격 시각 검사](TIMBRE-TRACKING.md)에 있다.

### 실제 음원 기반 평가

평가 도구는 실제 기존 음악에서 만든 통제된 변형과 사람의 실제 청취 정답을 별도로 집계한다.
원곡·볼륨 조절·부드러운 페이드는 미라벨 상태이며 정상 음원이라고 가정하지 않는다. 라벨은
실제 변형이 있는 시간 구간에만 붙이고, 원본·변형 PCM과 입력·보고서의 크기·SHA-256을 연결한다.

```bash
uv run python scripts/build_rhythm_evaluation.py \
  --source existing.wav --output-dir .runtime/new-calibration-corpus \
  --split calibration --source-family song-a --clip-seconds 60
uv run python scripts/evaluate_rhythm_quality.py \
  --manifest .runtime/new-calibration-corpus/corpus.json \
  --output-dir .runtime/new-calibration-run
```

새 출력 폴더만 사용한다. 평가에서는 겹친 경고를 정답 하나에 여러 번 적중 처리하지 않으며,
예측·정답 각각 50% 이상을 덮는 구간을 일대일로 연결한다. 미검토·불확실 구간은 제외하고,
`unknown`·정보 수준에 머문 결함은 누락으로 센다. 잘린 보고서는 평가 불가로 표시한다.
실패·미실행·평가 불가 사례를 전체 결함 분모에 남긴 보수적 recall도 기록한다. 같은 원본 해시나
곡 계보를 조정용과 별도 평가용에 나눠 넣을 수 없다. 미라벨 자료는 정확도를 만들지 않는다.
별도 평가용 사례는 코드와 원본의 해시를 먼저 고정한 기록(`--freeze`, `--frozen-protocol`)이 있어야 실행된다.

2026-10-09 기존 9곡으로 평가했다. 조정에 쓴 5곡과, 코드를 고정한 뒤 처음 평가한 4곡을 따로 적는다.
새 엔진 생성과 사용자 청취 요청은 하지 않았다.

| 통제 변형 | 조정용 5곡 | 처음 평가한 4곡 |
| --- | --- | --- |
| 50/90/130 ms 시간 교란, v3 | 0/60 | 0/48 |
| 같은 교란, v4 (정답과 서로 50% 이상 겹친 경고) | 44/60 | 18–22/48 |
| 같은 교란, v4 (경고 전체가 편집 구간 안) | 18/60 | 10/48 |
| 부드러운 템포 변화에 대한 v4 경고 | 0/5 | 0/4 |
| 원곡·음량·페이드 파일의 v4 경고 | 0/15 | 0/12 |
| 전체 믹스 디지털 끊김 0.1–1.5초 검출, v3·v4 동일 | 15/20 | 7/16 |
| 같은 끊김에 쉼 계획을 선언했을 때의 경고 | 0/20 | 0/16 |

범위로 적은 값은 변형 방법 두 가지(음높이가 함께 변하는 재표본화, 음높이를 유지하는 중첩 합성)의 결과다.
근거가 없어 판단을 보류한 구간은 조정용 12개, 처음 평가한 곡 7개이며 적중으로 세지 않는다.
처음 평가한 4곡의 적중은 곡마다 0/12에서 11/12까지 갈렸다. 경고 0개를 의도 100% 구분으로 해석하지 않는다.
자연 발생 모델 오류와 의도된 표현에 대한 사람의 구간 정답은 없어 자연 음악 precision·recall은 미측정이다.
전체 기록은 `.runtime/rhythm-timing-evaluation-20261009/report.md`에 있고 Git에 넣지 않는다.

2026-10-07 `City Lights · 3분 · XL turbo`의 `candidate_d4944016970f49e6b2c6ad998f72ec4d`
(앱의 버전 1 › 수정 1)에 직접 UMXHQ 보컬·드럼 분석을 적용했다. 720개 보컬·반주 창과
드럼 onset 근거를 원본 해시에 연결하고 새 검사 보고서를 저장했다. 반주 끊김 의심 18곳,
예를 들어 50.25–50.75초의 주변 대비 반주 감소 16.86 dB를 표시했다. 이 값은 오류 확정이 아니다.
전곡 박자 불안정은 지지 근거가 부족해 `unknown`으로 남겼으며 정상이라고 승인하지 않았다.
모델 분석은 실제로 실행했고 프로세스 종료 후 메모리를 반환했다. 추가 음원 생성은 하지 않았다.
검사 보고서는 프로젝트 `artifacts/analysis/job_a5221ddeaa07495a93dbce36decabdb7.json`, 실행·분리 자료는
`.runtime/rhythm-diagnostics-20261007/`이다.

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

CLI와 앱의 `repaint`도 기본 `--quality auto`이며, 전체 수정 WAV에 PCM·리듬·받아쓰기 검사를
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
연주곡 `[Instrumental]`도 가사 모델을 설치·로드하지 않고 FFmpeg·PCM·리듬을 검사한다.

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

아래 기존 실측은 리듬·프레임 검사 추가 전의 기록이다. 새 리듬 검사로 새 ACE 추론을
검증했다는 근거로 사용하지 않는다. 기존 WAV의 DSP 분석과 새 모델 추론 검사는 구분한다.

리듬 검사를 포함한 새 추론 실행기는 `scripts/smoke_rhythm_quality.py`다. 별도로 켜 둔
XL turbo 서버에서 120 BPM·4/4·30초 연주곡을 한 번 새로 생성한다. 원본과 정리본 각각의
분석 SHA-256·프레임 관측값·청취 미승인 상태를 검사하고, 새 출력 디렉터리에 기록한다.

```bash
./scripts/start_ace_api.sh
# 다른 터미널에서, 존재하지 않는 출력 디렉터리를 지정한다.
uv run python scripts/smoke_rhythm_quality.py --output-dir .runtime/rhythm-smoke-new
```

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

## 리듬 확장 검증 (2026-10-07)

2026-10-07 ACE-Step XL turbo + 4B LM에서 요청 120 BPM·4/4의 새 30초 연주곡을 생성했다. 최종 코드로 실행한 생성·검사·재생본 정리는 117.9초였으며 원본·정리본의 크기·
SHA-256, 각 파일의 프레임 관측값 1,499개, 사람 청취 `unreviewed`를 확인했다.
강한 30 BPM 반복을 요청 120 BPM과 다른 박자 단위로 해석할 수 있어 `tempo_ambiguity`를
남겼다. 음량 변화도 청취 경고로 남았으며 실제 BPM·음악성이 정상이라고 승인한 결과가 아니다.
리포트는 `.runtime/rhythm-qc-20261007/real-generation-final/report.json`이다.
기존 30초·180초 음원도 읽기 전용으로 분석했고 원본 해시가 유지됐다.
검증용 ACE 서버는 종료했다. 다른 로컬 모델이 실행 중일 때의 쓰로틀링은 측정하지 않았으므로
실제 추론 검증은 다른 모델 작업이 끝난 뒤 따로 실행한다.

## 연구 근거·라이선스·남는 범위

onset 반복의 자기상관과 프레임 스펙트럼 분석 원리는
[librosa 박자 분석](https://librosa.org/doc/0.11.0/generated/librosa.beat.beat_track.html)과
[스펙트럼 중심 주파수 설명](https://librosa.org/doc/0.11.0/generated/librosa.feature.spectral_centroid.html)을
참고했다. 현재 구현은 NumPy 기반이며 librosa 모델이나 실행 환경을 사용하지 않는다.
타격 성분의 근사는 [AudioLabs의 median HPSS 설명](https://www.audiolabs-erlangen.de/resources/MIR/FMP/C8/C8S1_HPS.html)을,
학습된 드럼 추정은 [Open-Unmix 공식 설명](https://sigsep.github.io/open-unmix/)과
[UMXHQ 원 저자 배포](https://zenodo.org/records/3370489)를 따른다. 신뢰도 수치는 관측 지지 강도이며
의도치 않은 음악 오류의 확률로 교정된 값이 아니다.

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

리듬 검사는 템포·비트의 관측과 확인할 구간을 제공한다. 멜로디·화성·실제 박자표·마디 강세·
가수 음색의 일관성, 감정 표현, 자연스러운 발음, repaint 연결부는 이 검사가 해결했다고 주장하지 않는다.
자동 보컬 재믹스·강제 음절 절단·주관적인 장르 점수는 구현하지
않았다. ACE의 DiT Lyrics Alignment Score는 현재 pinned v0.1.8 REST/MLX 경로에서 사용할 수
있는 응답으로 연결돼 있지 않아 자동 기준에 쓰지 않는다. 구조·태그 준비와 측정 가능한 결함의
선별은 자동화하되, 추천 결과의 표현과 음악성은 사람이 듣고 평가한다.
