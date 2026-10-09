# 반복 대조 타격 시각 검사

`src/local_music_engine/timbre_tracking.py`(`timbre-timing-v2`)는 드럼처럼 갑자기 시작되는 소리(타격)가
같은 곡의 다른 반복과 비교해 빠르거나 늦은 구간을 찾는다. 모델 없이 PCM만 읽으며 음원을 바꾸지 않는다.
`rhythm-diagnostics-v4`의 `hitTiming` 항목으로 생성 후 자동 검사에 들어 있다.

결과는 **청취 확인용**이다. 경고가 있어도 자동으로 다시 만들지 않고(`retryEligible=false`),
추천 순위와 사람 청취 상태(`unreviewed`)를 바꾸지 않는다.

## 측정 방법

1. 10 ms 간격, 46 ms 창으로 8개 주파수 대역의 음량 상승을 구한다. 창 길이는 샘플레이트와 무관하게 같다.
2. 곡 전체의 평소 타격 크기에 비해 뚜렷한 피크만 타격으로 측정한다.
3. 대역별 상승 패턴의 자기상관으로 곡이 되풀이되는 간격(1–24초)을 찾는다. 템포·박자표를 가정하지 않는다.
4. 타격마다 공격음 모양(앞 30 ms–뒤 50 ms)을 그 간격만큼 떨어진 앞뒤 반복의 같은 자리(±0.3초)와 맞춰,
   반복마다 "이 타격은 그 반복보다 얼마나 빠르거나 늦은가"를 얻는다.
5. 여러 반복이 같은 값(±15 ms)에 동의할 때만 그 타격의 변위로 채택한다.
6. 연속된 타격의 변위에 부드러운 곡선(±2초 국소 2차식)을 맞춘다. 곡선에서 15 ms 이상 벗어난 타격이
   5개 이상, 1초 이상 이어지면 경고한다. 곡의 측정 잡음이 크면 15 ms 대신 잡음의 4배를 기준으로 쓴다.

반복되는 변주를 오류로 보지 않기 위한 규칙이 있다.

- 8초 이상 떨어진 반복 3개 이상이 "제자리"라고 하면 그 타격은 제자리다.
- 동의하는 반복에는 앞쪽과 뒤쪽이 모두 있어야 하고 8초 이상 떨어져 있어야 한다.
- 곡선에서 120 ms 넘게 벗어난 타격은 다른 타격으로 보고 근거에서 뺀다.

## 세 가지 시각의 구분

| 이름 | 무엇인가 | 필드 |
| --- | --- | --- |
| 모델 예측 박자 | 학습 모델의 추정. 이 검사는 쓰지 않는다 | `modelInference=false`, `modelPredictedBeatTimesSeconds=[]` |
| 실제 측정 시각 | PCM에서 측정한 공격음 피크 | `measuredOnsetTimesSeconds`, `hits`의 `measuredTimeSeconds` |
| 예상 곡선 | 측정한 타격에 맞춘 곡선. 측정도 모델도 아니다 | `expectedTimesSeconds`, `expectedTimingKind` |

측정 시각은 창 중심의 피크이며 샘플 단위의 물리적 타격 시각이 아니다. 빠진 타격을 격자로 채우지 않고,
요청 BPM·박자 모델·평가 정답을 입력으로 쓰지 않는다.

## 결과 읽기

| 표시 | 뜻 |
| --- | --- |
| 타격 시각 흔들림 의심 (`repeated_hit_timing_shift_suspected`, 경고) | 여러 타격이 연달아 다른 반복보다 빠르거나 늦다. 들어서 확인한다 |
| 완만한 템포 변화 관찰 (`smooth_timing_change_observed`, 정보) | 2초 이상에 걸쳐 50 ms 이상 부드럽게 달라졌다. 경고가 아니다 |
| 판단 어려움 (`unknown`) | 반복되는 리듬이 부족해 비교할 근거가 없다. 정상이라는 뜻이 아니다 |

`supportedFraction`은 근거를 확보한 시간의 비율이다. 근거가 없는 구간에서 경고가 없는 것은 판단 보류다.
`confidence`는 동의한 근거의 강도이며 결함일 확률이 아니다.

## 검증 결과 (2026-10-09)

실제 음악에 타격 시각을 50–130 ms 미는 통제 변형을 넣고 찾는지 측정했다. 조정에 쓴 5곡과, 코드를
고정한 뒤 처음 평가한 4곡을 따로 적는다. 채점 규칙은 둘이다. **엄격**은 경고 전체가 편집 구간 안에
있어야 하고, **국소**는 경고와 정답이 서로 50% 이상 겹치면 된다. 범위는 변형 방법 두 가지의 결과다.

| | 이전 기본(v3) | 조정용 5곡 | 처음 평가한 4곡 |
| --- | --- | --- | --- |
| 시간 교란 적중, 국소 | 0 | 44/60 | 18–22/48 |
| 시간 교란 적중, 엄격 | 0 | 18/60 | 10/48 |
| 근거가 없어 판단 보류 | 전부 | 12/60 | 7/48 |
| 부드러운 템포 변화에 경고 | 0 | 0/5 | 0/4 |
| 원곡·음량·페이드 파일에 경고 | 0 | 0/15 | 0/12 |

라벨이 없는 기존 음원 119개·146분에서는 경고가 0개였다. 정답이 없으므로 평소 곡에서 조용하다는 뜻이다.

## 한계

- 독립된 검증 표본은 곡 4개다. 곡별 적중이 0/12에서 11/12까지 갈려 한 숫자로 일반화할 수 없다.
- 통제 변형은 생성 모델이 실제로 내는 박자 오류와 같다는 보장이 없다. 자연 음악의 오류에 대한
  사람의 구간별 정답은 없고, 자연 음악 정확도는 측정하지 않았다.
- 반복되는 리듬이 없는 곡, 한 번만 나오는 구간, 반복마다 똑같이 틀린 타격은 판단할 수 없다.
- 경고 구간은 실제 문제 구간보다 최대 2초가량 넓을 수 있다.
- 보컬·멜로디·화성 오류는 대상이 아니다.

수치의 전체 기록과 재현 명령은 `.runtime/rhythm-timing-evaluation-20261009/report.md`에 있다.
이 폴더는 Git에 넣지 않는다.

## 다시 평가하기

```bash
# 통제 음원 만들기. --time-method overlap_add는 음높이를 유지한다.
uv run python scripts/build_rhythm_evaluation.py --source existing.wav \
  --output-dir .runtime/new-corpus --split calibration --source-family song-a --clip-seconds 60

# 평가. 출력 폴더는 새 경로여야 한다.
uv run python scripts/evaluate_rhythm_quality.py \
  --manifest .runtime/new-corpus/corpus.json --output-dir .runtime/new-run
```

새 곡으로 검증할 때는 통제 음원을 만들기 **전에** 코드와 원본을 고정한다. `holdout` 사례는 고정 기록
없이 실행되지 않고, 실행 전·중·후에 해시를 다시 확인한다.

```bash
uv run python scripts/evaluate_rhythm_quality.py --freeze .runtime/new-freeze.json \
  --calibration-family song-a --holdout-source song-b=/absolute/path/song-b.wav
uv run python scripts/build_rhythm_evaluation.py --source /absolute/path/song-b.wav \
  --output-dir .runtime/new-holdout-corpus --split holdout --source-family song-b --clip-seconds 60
uv run python scripts/evaluate_rhythm_quality.py --manifest .runtime/new-holdout-corpus/corpus.json \
  --frozen-protocol .runtime/new-freeze.json --output-dir .runtime/new-holdout-run
```

이미 평가한 9곡은 모두 조정용이다. 이 검사를 고치면 평가한 적 없는 곡으로 다시 검증한다.

## 채택하지 않은 방법

- **음색 줄 추적(`timbre-timing-v1`)**: 비슷한 음색의 타격을 한 줄로 이어 간격을 봤다. 교란 구간의
  타격 1,203개 중 판단에 쓸 수 있었던 것이 10개여서 실제 음악 검출이 0/12였다.
- **학습 박자 모델(Beat This!)**: 예측 박자가 밀린 타격을 따라가지 않고 고른 격자 쪽으로 정리됐다.
  검출이 개선되지 않았고 의도된 템포 변화에 오경고가 생겼다.
- **NMF 음색 분해**: 조정용 시간 교란에서 검출 0개였다.

세 방법의 코드와 모델 실행 환경은 저장소에서 지웠다.
