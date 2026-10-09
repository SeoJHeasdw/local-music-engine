# local-music-engine

Apple Silicon Mac에서 곡을 만들고, 들어 보고, 가사와 편곡을 다듬는 로컬 제작 엔진과 데스크톱 앱이다.
앱의 기본 생성 엔진은 **ACE-Step 1.5 XL turbo(4B DiT) + 4B 5Hz LM · native MLX**다. 설명에서 제목·스타일·가사
초안을 준비하고, 감성 힙합·신나는 팝 제작 규칙을 골라 새 곡을 만든다. 파형에서 고른 구간만 다시 만들 수
있다. 한국어와 영어 가사를 지원한다. 발음과 음악적 완성도는 생성한 음원을 직접 들어 확인한다.

원본과 모든 버전을 보존한다. 요청·생성·자동 검사·사람 평가·최종본 선택의 정본은
`project.json`이다. 자동 검사 통과와 사람 청취 승인은 별도이며, 새 음원은 `unreviewed`다.
Music 3로 만든 곡도 재생·비교·평가·선택·내보낼 수 있다.

## 준비

- Apple Silicon Mac(36GB에서 실측), [uv](https://docs.astral.sh/uv/), Node.js, Git
- [FFmpeg](https://ffmpeg.org/): 로컬 받아쓰기, true peak·음량 측정
- 선택: Ollama 또는 OpenAI 호환 loopback LLM. 자유로운 가사 초안과 피드백 해석에 사용

```bash
chmod +x scripts/*.sh app.sh
./scripts/bootstrap_ace.sh  # 고정 판본의 ACE-Step 실행 환경 준비
./app.sh                    # 프로젝트 Python·Electron 환경 준비, 앱 빌드 및 실행
```

환경은 루트 `.venv`, ACE의 `.runtime/ace-step-1.5/.venv`, 품질 검사의 `.runtime/quality/.venv`로 분리한다.
모델은 `.runtime/models/`에 둔다. 엔진을 처음 켤 때 없는 모델(XL turbo 약 20GB, 4B LM 약 8GB와 기본 묶음)을
Hugging Face에서 받는다. 음원·가사·프로젝트·모델·로그는 Git에 넣지 않는다. 생성과 품질 검사에서
음원·가사를 외부 API에 업로드하지 않는다.

## 음악 엔진

곡의 멜로디·구성은 4B 5Hz LM이 계획하고, DiT가 소리로 그린다. 설정에서 DiT를 고른다.

| DiT | 특징 | 30초 곡 DiT 시간 | 엔진 상주 메모리 |
| --- | --- | --- | --- |
| `acestep-v15-xl-turbo` (기본) | 같은 곡을 더 큰 모델로 그린다. 블라인드 청취에서 끊김이 적고 완성도가 높았다 | 13초 | 26.2GB |
| `acestep-v15-turbo` | 더 빠르다. 블라인드 청취에서 더 멜로디컬했지만 렉·끊김이 있었다 | 7초 | 23.7GB |

같은 seed·LM에서 turbo의 출력은 기존과 비트 단위로 같다. XL은 `scripts/ace_api_server.py`가 체크포인트를
MPS에 올리기 전에 MLX로 옮기고 행렬 가중치를 BF16으로 둬 이 Mac에 들어가게 한다(ACE가 CUDA에서 쓰는 정밀도).
모든 조건에서 생성 마지막의 VAE 디코딩이 약 5초 동안 메모리를 13GB 더 쓰고, 3분 곡 peak은 약 38GB였다.
SFT 계열(`acestep-v15-xl-sft`)은 같은 런처로 보컬이 나오지만 청취에서 음질이 깨져 쓰지 않는다.
측정과 근거는 [인수인계](docs/HANDOFF.md)에 있다.

앱은 `scripts/start_ace_api.sh`로 `127.0.0.1:18001` 서버를 켜고, 자기가 켠 서버만 종료한다.
이미 켜 둔 ACE 서버는 외부 소유로 사용한다. 다른 엔진이나 설정과 다른 DiT·LM이 응답하면 준비된
서버로 인정하지 않는다. 한 번에 한 곡을 계산한다.

큰 로컬 AI 도우미를 부를 때 앱은 자기가 켠 음악 엔진을 잠시 끄고, 답을 받으면 다시 켠다.
외부에서 켠 서버는 임의로 종료하지 않는다. 곡을 만드는 중에는 도우미를 호출하지 않는다.
CLI에서 큰 도우미를 부를 때는 엔진을 직접 끈다.

인증 키는 `.runtime/ace-api-key`의 사용자 전용 파일(0600)로 main·CLI·서버가 공유한다.
별도 키는 `MUSIC_ENGINE_ACE_API_KEY`, 키 파일은 `MUSIC_ENGINE_ACE_API_KEY_FILE`로 지정한다.
renderer에는 키를 전달하지 않는다. 서버 앞단은 인증된 요청만 받고 브라우저 Origin을 거부한다.

MiniMax Music 3 엔진 코드(`scripts/start_music3_api.sh`, CLI의 기본 `--engine`)는 남아 있지만 앱에서는 쓰지
않으며, 모델은 삭제했다. 쓰려면 `./scripts/bootstrap_music3.sh`로 약 28.5GB를 다시 받는다. CLI로 ACE를
쓸 때는 `--engine ace-step --model acestep-v15-xl-turbo`를 명시한다.

## 앱

| 화면 | 하는 일 |
| --- | --- |
| 사이드바 | 위쪽 접기(⌘\)·뒤로(⌘[)·앞으로(⌘]), 새 곡 만들기, 내 곡, 최근 곡 바로 열기. 가장자리를 끌어 너비 조절(두 번 누르면 기본) |
| 내 곡 | 곡 목록과 검색, 상태·버전·길이, 다른 곡 폴더 열기, Finder에서 보기 |
| 새 곡 만들기 | ① 원하는 곡을 말로(가사가 있으면 선택해서 함께 넣으면 AI는 제목·스타일만 채운다), 악기(선택)를 고르면 그 태그를 스타일에 함께 보낸다 → AI 초안 ② 제목·스타일 프롬프트·가사 다듬기(× 로 닫아도 쓴 내용은 남는다). 장르 추천·제작 규칙·BPM·전개 미리보기는 ‘세부 조정’, 실제로 보낼 값은 ‘엔진에 보낼 내용’에 접어 둔다 |
| 곡 화면 | 위: 재생·파형·구간 선택·비교. ⌘ + 스크롤·두 손가락 벌리기·+/−로 최대 48배 확대하고 옆으로 밀거나 아래 전체 띠를 끌어 이동하며, 시작·끝을 0.01초 단위로 적거나 0.1초씩 옮긴다. 가운데: 버전 목록(듣기·좋아요·별로·최종본). 아래: 만든 프롬프트·가사·자동 검사·평가·메모 탭. 가사 탭의 ‘가사 고치기’는 파형에서 고른 구간만 고친 가사로 다시 만들거나, 고친 가사로 새 전체 버전을 만든다(원래 버전은 남는다). 오른쪽: 고치기(곡 전체/구간 → 요청 문장이나 ‘악기 더하기·빼기’ → 수정안 → 만들기)와 고친 기록. 장르 추천 안내에서 온 악기는 여기서 뺄 수 없다고 표시한다 |
| 설정 | 창 크기·최대화·전체 화면은 다음 실행 때 그대로 연다(처음엔 화면을 채운다). 사이드바 아래 음악 엔진 카드에서 연다(⌘,). ACE 상태·시작·종료·기록, DiT 선택, AI 도우미, 저장 위치와 생성 기본값 |

이전 설정(Music 3 또는 첫 ACE 판)은 최초 실행 때 ACE XL turbo로 옮기고 원본을 백업한다
(`settings-before-ace-xl.json`). 저장 위치·AI 도우미·내보내기 위치와 기존 곡 데이터는 보존한다. 요청 길이
상한은 **300초**다. 실제 길이는 artifact에 기록하고 무음을 덧붙여 성공한 것처럼 표시하지 않는다.

구간 다시 만들기는 고른 구간만 새로 만들고 원본은 그대로 둔다. 앱이 켠 DiT로 요청하므로 turbo로 만든 옛
버전도 XL로 고칠 수 있고, 어느 모델이 실행됐는지 요청에 남는다. 음원 참조 커버는 CLI(`cover`)에서만 만든다.
특정 버전을 바탕으로 새로 만들면 그 버전의 **가사·스타일·제작 규칙**을 사용한다.

## 제작 규칙과 곡 계획

감성 힙합은 84 BPM·A minor·4/4, 신나는 팝은 120 BPM·C Major·4/4를 제안한다. 직접 입력한
음악 정보가 우선한다. 이 값은 제작 안내이며 실제 음원의 엄격한 보장이 아니다.

박자 유지, 코드 반복, 단출한 악기 구성, 또렷한 보컬, 깨끗한 음색, 단순한 구성의 여섯 규칙을
개별 선택할 수 있다. **구간별 전개 만들기**와 **가사 호흡 나누기**는 추가 옵션이다. 구간의 상대
강약과 호흡을 안내하며, 긴 줄은 기존 띄어쓰기·문장부호에서만 나눠 단어와 순서를 보존한다.
원문·준비본·계획·변경 내용은 따로 저장한다. 연주곡에는 보컬·호흡 규칙을 적용하지 않는다.
**기억에 남는 선율**과 **보컬 감정 살리기**도 선택할 수 있다. 감성 힙합 프리셋은 편안한
싱잉랩 구절에서 노래하는 후렴으로, 신나는 팝은 밝은 선율과 탄력 있는 프레이징으로 안내한다.
선율 규칙은 도입에서 후렴의 주제를 예고하고, 후렴의 리듬·선율 동기와 응답 구절을 반복하도록 안내한다.

ACE에는 직접 쓴 스타일 뒤에 프리셋·규칙 문장을 쉼표로 이은 짧은 caption(약 650자 한도)과 `[Verse - restrained]`
처럼 구간 안내를 붙인 가사를 보낸다. 화면은 이 한도를 넘으면 알린다.

Music 3(CLI)에는 안내를 모델 학습 형식의 구조화된 음악 설명(caption v4)으로 보낸다. MiniMax가 공개한
[caption 예시 1,000개](https://github.com/MiniMax-AI/MiniMax-Music3/tree/main/skills/music-caption-rewriter)는
모두 괄호 없는 `Global Metadata / Vocal Details / Arrangement` 제목과 고정된 13개 항목
(`Basic Attributes: bpm is 84. key is A, and scale is minor. Hip-Hop / Melodic Rap.`,
`Vocal Gender & Timbre: Singer A (Male). …`, `Primary:`, `Groove & Foundation Progression:` 등)을 쓴다.
프리셋은 이 항목을 서술형으로 채우고, 규칙은 해당 항목에 문장을 더한다. 직접 쓴 스타일은 곡의 정체성으로
`Basic Attributes`에 들어가며, 이미 같은 형식으로 쓴 스타일은 항목별로 그대로 쓴다. 보컬 성별은 직접
쓴 스타일에서만 읽는다. 조성은 예시와 같은 표기(Bb·Eb·Ab·F#·C#)로 맞춘다. `Duration:`·
"가사를 순서대로 부르라" 같은 명령문은 보내지 않는다. 구간 표현 지시는 편곡 항목에 옮기고 가사에는
`[Verse]`, `[Chorus]` 같은 단순한 태그를 쓴다. ACE 전용 LM·샘플링 옵션은 전달하지 않는다.
추정 시간·마디·음절 수는 기록과 미리보기에 남긴다. 지시의 효과는 후보별로 확인하며 음악성을
보증하지 않는다. [Music 3 입력 계약](https://huggingface.co/MiniMaxAI/MiniMax-Music3)을 따른다.

AI 도우미가 없으면 로컬 규칙으로 수정 가능한 초안을 제공한다. 초안을 위해 ACE 모델을 올리거나
Music 3에 없는 작사 API를 호출하지 않는다. 자유로운 작사는 선택한 로컬 도우미를 쓸 수 있다.

## CLI

앱과 CLI는 같은 저장·생성·검사 흐름을 쓴다. 명령마다 JSON 한 개를 출력한다.

```bash
uv run music-engine production-rules
uv run music-engine draft --query "늦은 밤 감성 힙합" --duration 30 --vocal-language en
uv run music-engine init projects/my-song --title "내 노래" --lyrics-file lyrics.txt \
  --style "emotional hip hop, warm piano, clear male melodic rap" --duration 60
uv run music-engine generate projects/my-song --seeds 101 --engine ace-step --model acestep-v15-xl-turbo
uv run music-engine status projects/my-song
uv run music-engine library --dir projects
```

`init`·`revise`·`draft`의 `--production-rules-json`으로 화면과 같은 규칙을 전달한다.
CLI의 기본 `--engine`은 아직 Music 3이므로 ACE로 만들 때는 `--engine ace-step`을 붙인다. 품질 기본값은
`--quality auto --quality-attempts 4`다. 버전마다 첫 생성·재시도를 합쳐
최대 네 번이며 검사 불확실성만으로 다시 만들지 않는다. 모든 시도의 입력과 예산을 첫 batch에
고정하고, 명시적인 재개도 예산을 늘리지 않는다. 자동 추천은 사람의 최종본 선택과 분리한다.
`auto`·`audio`는 생성 요청의 BPM·박자표를 기준으로 반복 박동·구간 드리프트·박자 후보 간격과
프레임별 음량·스펙트럼도 자동 검사한다. 절반·두 배·마디 단위의 해석 차이를 함께 기록하며, 마디 길이는
요청 박자표에서 계산한 예상값이다. 리듬 경고는 청취 확인용으로 남기고 자동 시간 보정이나 재생성의
근거로 쓰지 않는다. 원본과 사람 청취 `unreviewed` 상태를 보존한다.
추가 구간 진단은 타격 성분의 박자 간격과 분리한 보컬·반주의 연속성을 따로 비교한다.
검사 탭에서 박자 불안정 의심·타격 시각 흔들림 의심·반주 끊김 의심·타격 소리 쉼·판단 어려움을 구분하며,
의도된 편곡인지 자동 승인하지 않는다. 모델 없는 타격 성분 추정만으로 반주 끊김을 판단하지 않는다.
[자동 품질 흐름과 한계](docs/AUTO-QUALITY.md)를 참고한다.

타격 시각 검사는 측정한 타격을 같은 곡의 다른 반복과 비교한다. 모델을 쓰지 않으며, 반복되는 리듬이
부족한 곡은 판단 어려움으로 남긴다. 방법과 검증 결과는 [반복 대조 타격 시각 검사](docs/TIMBRE-TRACKING.md)에 있다.

```bash
uv run music-engine generate projects/my-song --seeds 101 --quality-attempts 2
uv run music-engine generate projects/my-song --seeds 102 --quality audio
uv run music-engine generate projects/my-song --source-candidate-id <candidate-id> \
  --seeds 103 --style "fuller chorus, clear lead vocal"
uv run music-engine revise projects/my-song --title "새 제목" --duration 120
uv run music-engine repaint projects/my-song --engine ace-step --candidate-id <candidate-id> \
  --start 41 --end 58 --seed 104 --strength strong --model acestep-v15-xl-turbo
uv run music-engine resume projects/my-song --engine ace-step --job-id <batch-job-id>
uv run music-engine recover projects/my-song
uv run music-engine review projects/my-song <candidate-id> --status listened --note "후렴 좋음"
uv run music-engine select projects/my-song <candidate-id>
uv run music-engine undo-selection projects/my-song
uv run music-engine export projects/my-song --output ~/Music/my-song.wav
uv run music-engine analyze-rhythm path.wav --bpm 84 --time-signature 4/4
```

`--source-candidate-id`는 그 버전의 입력으로 새 전체 곡을 만든다. 새 생성은 과거 결과를
묵시적으로 재사용하지 않는다. `resume`만 같은 계보·요청 fingerprint·파일 존재·크기·SHA-256이
모두 일치하는 완료 결과를 재사용한다. export는 프로젝트 밖의 새 파일 이름만 허용한다.

`repaint`는 고른 구간만 새로 만든 새 버전을 남기며 원본은 바꾸지 않는다. 구간 밖도 조금 달라질 수
있다. ACE 실측 실행기는 `scripts/smoke_real_engine.py`와 `scripts/compare_production.py`다.
`analyze-rhythm`은 기존 WAV를 읽어 JSON으로 분석하며 음원이나 프로젝트를 바꾸지 않는다.
`analyze-rhythm --diagnostics`는 모델 없이 타격 성분을 추가 분석한다. 분리 결과가 없으면
반주 연속성은 판단 어려움으로 남는다. `inspect-rhythm`은 기존 후보에 새 분석 artifact·revision을
추가하며 원래 음원·기존 QC·사람 선택과 평가는 보존한다.

```bash
uv run music-engine analyze-rhythm path.wav --bpm 84 --diagnostics
uv run music-engine inspect-rhythm projects/my-song --candidate-id <candidate-id>
```

현재 v4 구간 진단은 전곡 프롬프트 단어만으로 경고를 낮추지 않는다. 같은 음원 해시에 연결한
`--intent-json`으로 구간별 템포 변화·반주 쉼 계획을 대조할 수 있다. 템포 계획 안의 불규칙한
흔들림과 계획 밖의 경고는 남기며, 표현의 의도나 모든 모델 오류를 자동으로 확정하지 않는다.
입력 형식과 판별 범위는 [자동 품질 검사](docs/AUTO-QUALITY.md#구간별-의도-대조와-검사-범위)를 참고한다.
곡 중간의 급격한 디지털 무음도 전체 PCM에서 측정하고, 원본 믹스와 분리 자료의 불일치·
반복 형태를 추가 근거로 남긴다. v1·v2·v3 과거 계획의 검사 범위는 유지한다. 실제 기존 음악의
통제된 변형으로 평가한 결과, 코드를 고정한 뒤 처음 평가한 4곡에서 끊김 16개 중 7개,
시간 교란 48개 중 18–22개를 찾았고 부드러운 템포 변화·원곡에는 경고가 없었다. 곡마다 차이가 크며,
모든 모델 오류나 의도를 정확히 판별하는 수준으로 보증하지 않는다.

기존 품질 환경의 UMXHQ는 보컬과 잔여 반주 에너지의 시각별 근거도 남긴다. 드럼 추정 모델은
약 35.6 MB이며 선택적으로 준비한다. 이미 검증된 가중치가 있으면 같은 분석 과정에서 함께 사용한다.
다음 `separate` 명령은 음원을 변경하거나 받아쓰기 모델을 올리지 않고 분리 분석만 수행한다.

```bash
.runtime/quality/.venv/bin/python scripts/quality_worker.py setup --drums
.runtime/quality/.venv/bin/python scripts/quality_worker.py separate --audio /absolute/path.wav > separated-energy.json
uv run music-engine analyze-rhythm /absolute/path.wav --diagnostics --separation-json separated-energy.json
uv run music-engine inspect-rhythm projects/my-song --candidate-id <candidate-id> --separation-json separated-energy.json
```

## 데이터와 검사

- `project.json`은 같은 디렉터리의 임시 파일을 fsync한 뒤 원자적으로 교체한다.
- 완료 artifact는 파일 존재·크기·SHA-256과 실제 sample 메타데이터로 검증한다.
- 새 생성·후처리·export는 새 artifact다. 원본과 과거 revision을 덮지 않는다.
- 새 후보는 자동 검사 결과와 관계없이 사람 청취 `unreviewed`다.
- renderer는 main이 확인한 곡·버전 ID만 쓰며 임의 파일 접근·명령 실행 API를 받지 않는다.
- 엔진과 도우미는 loopback만 허용하고 환경 프록시·HTTP 리다이렉트를 따르지 않는다.
- 기존 `local-tts-engine`의 환경·모델·개인 음성을 사용하거나 변경하지 않는다.

첫 보컬 검사에서 품질 환경에 MLX Whisper·UMXHQ 가중치 약 1.65GB를 준비한다. 미리 준비하려면
`scripts/bootstrap_quality.sh`를 실행한다. 분석 실패 시 음원을 보존하고 불확실성을 표시한다.
연주곡은 받아쓰기 모델을 올리지 않는다. 리듬·프레임 분석은 별도 모델 없이 실행한다.
PCM·음량·리듬·가사 비교는 측정값과 확인할 구간을 찾는 도구이며
선율의 매력이나 음악성을 자동 승인하지 않는다.

```bash
uv sync --python 3.12.12 --group dev
uv run python -m compileall -q src tests
uv run pytest
npm run check:app
npm run test:app
npm run build:app
```

생성과 리듬 검사를 함께 실측하려면 기본 XL turbo 서버에서 새 30초 연주곡을 한 번 만든다.
원본·정리본의 분석과 artifact 해시, 사람 청취 `unreviewed` 상태를 확인한다.

```bash
./scripts/start_ace_api.sh
# 다른 터미널에서, 존재하지 않는 출력 디렉터리를 지정한다.
uv run python scripts/smoke_rhythm_quality.py --output-dir .runtime/rhythm-smoke-new
```

실제 추론은 모델 없는 계약·회귀 검사와 따로 실행한다. 서버를 켜고 새 출력 폴더를 지정한다.
아래 검사는 영어 30초·한국어 30초·새 2절이 있는 영어 60초를 각각 한 번 생성하고, 자동 검사·
export·검증된 명시적 재개·원문 보존·사람 미청취 상태를 확인한다.

```bash
./scripts/start_music3_api.sh
# 다른 터미널에서
uv run python scripts/smoke_music3.py --output-dir .runtime/music3-smoke-new
```

좋다고 평가한 기존 후보를 기준으로 비교하려면 아래 실행기를 사용한다. 기준 파일의 존재·크기·
SHA-256을 확인해 새 프로젝트에 **재생용으로 명시적으로 복사**하고, 같은 가사·요청 길이·BPM·
조성·seed로 두 Music 3 후보를 각각 한 번 새로 생성한다. 원본 파형은 모델에 보내지 않는다.
곡 화면의 ‘비교할 버전’에서 같은 위치를 번갈아 듣고 평가할 수 있다. 보고서의 음악 기준 판정은
직접 듣기 전까지 `pending_human_listening`이며 자동 가사 검사 결과로 승인하지 않는다.

```bash
uv run python scripts/compare_music3_quality.py \
  --baseline-project projects/reference-song --baseline-candidate candidate_id \
  --project-dir projects/music3-comparison-new --output-dir .runtime/music3-comparison-new
```

## 문서

- [아키텍처와 데이터 계약](docs/ARCHITECTURE.md)
- [품질 검사와 청취 계약](docs/QUALITY.md)
- [자동 품질 관리·재시도·실제 검증](docs/AUTO-QUALITY.md)
- [모델·라이선스 결정](docs/DECISIONS.md)
- [현재 상태와 전환 기록](docs/HANDOFF.md)
- [초기 설계 계획](PLAN.md), [과거 ACE 실측](docs/P0-VALIDATION.md)
