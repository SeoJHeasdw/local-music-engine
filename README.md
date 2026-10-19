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

| DiT | 특징 | 30초 곡 DiT 시간 | 켜져 있는 동안 메모리 |
| --- | --- | --- | --- |
| `acestep-v15-xl-turbo` (기본) | 같은 곡을 더 큰 모델로 그린다. 블라인드 청취에서 끊김이 적고 완성도가 높았다 | 13초 | 26.2GB |
| `acestep-v15-turbo` | 더 빠르다. 블라인드 청취에서 더 멜로디컬했지만 렉·끊김이 있었다 | 7초 | 23.7GB |

같은 seed·LM에서 turbo의 출력은 기존과 비트 단위로 같다. XL은 `scripts/ace_api_server.py`가 체크포인트를
MPS에 올리기 전에 MLX로 옮기고 행렬 가중치를 BF16으로 둬 이 Mac에 들어가게 한다(ACE가 CUDA에서 쓰는 정밀도).
모든 조건에서 생성 마지막의 VAE 디코딩이 약 5초 동안 메모리를 13GB 더 쓰고, 3분 곡 peak은 약 38GB였다.
SFT 계열(`acestep-v15-xl-sft`)은 같은 런처로 보컬이 나오지만 청취에서 음질이 깨져 쓰지 않는다.
런처가 고친 내용과 근거는 `scripts/ace_api_server.py`의 docstring에 있다.

ACE는 곡을 만드는 동안에만 켜져 있다. 생성·커버·구간 수정·재개(앱도 이 CLI를 쓴다)는 `127.0.0.1:18001`에
아무것도 없으면 `scripts/start_ace_api.sh`로 서버를 켜고 모델이 올라올 때까지 기다린 뒤 쓴다(XL에서 약 46초).
그 서버는 기기 공용 생성 차선을 아무도 쥐지 않게 되면 1–2초 안에 스스로 꺼진다. 그래서 쉬는 동안 모델
메모리를 쥐지 않고, 생성이 죽어도 서버가 남지 않는다. 손으로 켠 ACE 서버는 그대로 쓰고 끄지 않는다.
다른 엔진이나 설정과 다른 DiT·LM이 응답하면 쓰지 않고, 생성이 켠 서버가 LM을 올리지 못했으면 바로 멈춘다. 한
번에 한 곡을 계산한다. 서버 기록은 `.runtime/logs/ace-server.log`에 남는다.

큰 로컬 AI 도우미는 음악 엔진이 꺼져 있을 때만 부른다. 곡을 만든 직후라면 엔진이 꺼질 때까지 잠시 기다리고,
손으로 켠 엔진이 떠 있으면 부르지 않는다. 곡을 만들기 전에는 도우미 모델을 메모리에서 내린다.

인증 키는 `.runtime/ace-api-key`의 사용자 전용 파일(0600)로 main·CLI·서버가 공유한다.
별도 키는 `MUSIC_ENGINE_ACE_API_KEY`, 키 파일은 `MUSIC_ENGINE_ACE_API_KEY_FILE`로 지정한다.
renderer에는 키를 전달하지 않는다. 서버 앞단은 인증된 요청만 받고 브라우저 Origin을 거부한다.

MiniMax Music 3 엔진 코드(`scripts/start_music3_api.sh`)는 남아 있지만 앱과 CLI 기본값에서는 쓰지 않으며,
모델은 삭제했다. 쓰려면 `./scripts/bootstrap_music3.sh`로 약 28.5GB를 다시 받고 CLI에
`--engine minimax-music3`를 명시한다.

## 앱

| 화면 | 하는 일 |
| --- | --- |
| 사이드바 | 위쪽 접기(⌘\)·뒤로(⌘[)·앞으로(⌘]), 새 곡 만들기, 내 곡, 최근 곡 바로 열기. 가장자리를 끌어 너비 조절(두 번 누르면 기본) |
| 내 곡 | 곡 목록과 검색, 상태·버전·길이, 다른 곡 폴더 열기, Finder에서 보기 |
| 새 곡 만들기 | ① 원하는 곡을 말로(가사가 있으면 선택해서 함께 넣으면 AI는 제목·스타일만 채운다), 악기(선택)를 고르면 그 태그를 스타일에 함께 보낸다 → AI 초안 ② 제목·스타일 프롬프트·가사 다듬기(× 로 닫아도 쓴 내용은 남는다). 장르 추천·제작 규칙·BPM·전개 미리보기는 ‘세부 조정’, 실제로 보낼 값은 ‘엔진에 보낼 내용’에 접어 둔다 |
| 곡 화면 | 위: 재생·파형·구간 선택·비교. ⌘ + 스크롤·두 손가락 벌리기·+/−로 최대 48배 확대하고 옆으로 밀거나 아래 전체 띠를 끌어 이동하며, 시작·끝을 0.01초 단위로 적거나 0.1초씩 옮긴다. 가운데: 버전 목록(듣기·좋아요·별로·최종본). 아래: 만든 프롬프트·가사·자동 검사·평가·메모 탭. 자동 검사 탭은 확인할 것만 시각·듣기 버튼과 함께 먼저 보여 주고 나머지 측정값은 접어 둔다. 가사 탭의 ‘가사 고치기’는 파형에서 고른 구간만 고친 가사로 다시 만들거나, 고친 가사로 새 전체 버전을 만든다(원래 버전은 남는다). 오른쪽: 고치기(곡 전체/구간 → 요청 문장이나 ‘악기 더하기·빼기’ → 수정안 → 만들기)와 고친 기록. 장르 추천 안내에서 온 악기는 여기서 뺄 수 없다고 표시한다 |
| 설정 | 창 크기·최대화·전체 화면은 다음 실행 때 그대로 연다(처음엔 화면을 채운다). 사이드바 아래 음악 엔진 카드에서 연다(⌘,). ACE 상태(쉬는 중·켜는 중·켜짐)·기록, DiT 선택, AI 도우미, 저장 위치와 생성 기본값 |

이전 설정(Music 3 또는 첫 ACE 판)은 최초 실행 때 ACE XL turbo로 옮기고 원본을 백업한다
(`settings-before-ace-xl.json`). 저장 위치·AI 도우미·내보내기 위치와 기존 곡 데이터는 보존한다. 요청 길이
상한은 **300초**다. 실제 길이는 artifact에 기록하고 무음을 덧붙여 성공한 것처럼 표시하지 않는다.

구간 다시 만들기는 고른 구간만 새로 만들고 원본은 그대로 둔다. 새 버전에서 바뀐 구간 밖은 고치기 전 버전의
샘플 그대로이며 곡 전체가 작아지지 않는다. 곡이 요청한 BPM과 다르게 연주되면 실제 템포로 다시 만들어
이음매의 박을 맞춘다. 앱이 켠 DiT로 요청하므로 turbo로 만든 옛
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

Music 3(`--engine minimax-music3`)에는 같은 안내를 MiniMax가 공개한 caption 형식(`Global Metadata /
Vocal Details / Arrangement`의 13개 항목)으로 옮겨 보낸다. 매핑은 `music3.py`에 있다.

AI 도우미가 없으면 로컬 규칙으로 수정 가능한 초안을 제공하며, 초안을 위해 음악 서버를 부르지 않는다.
자유로운 작사는 선택한 로컬 도우미를 쓸 수 있다. CLI의 `draft --engine ace-step`만 켜져 있는 ACE의
LM에 초안을 묻는다.

## CLI

앱과 CLI는 같은 저장·생성·검사 흐름을 쓴다. 명령마다 JSON 한 개를 출력한다.

```bash
uv run music-engine production-rules
uv run music-engine draft --query "늦은 밤 감성 힙합" --duration 30 --vocal-language en
uv run music-engine init projects/my-song --title "내 노래" --lyrics-file lyrics.txt \
  --style "emotional hip hop, warm piano, clear male melodic rap" --duration 60
uv run music-engine generate projects/my-song
uv run music-engine status projects/my-song
uv run music-engine library --dir projects
```

`init`·`revise`·`draft`의 `--production-rules-json`으로 화면과 같은 규칙을 전달한다.
CLI의 기본 엔진은 앱과 같은 ACE-Step XL turbo(`127.0.0.1:18001`, `acestep-v15-xl-turbo`)다. 생성 명령과
`draft --engine ace-step`은 ACE가 꺼져 있으면 켜서 쓰고, 끝나면 ACE가 스스로 꺼진다. 서버가 다른
DiT를 올렸으면 `--model`로 그 이름을 적는다.

`generate`는 기본으로 버전 하나를 새 seed로 만든다. 여러 개는 `--versions 2–4`로 고르거나, 같은 곡을 다시
만들려면 `--seeds 101,102`처럼 seed를 적는다(둘은 함께 쓰지 않는다). 쓴 seed는 `project.json`에 남는다.

품질 기본값은 `--quality auto --quality-attempts 4`다. 버전마다 첫 생성과 재시도를 합쳐 최대 네 번이며,
검사가 불확실하다는 이유만으로 다시 만들지 않는다. 다시 만드는 근거는 전체 무음, 클리핑, 큰 길이 차이, 크게
틀린 가사, 통째로 빠진 후렴·구절뿐이다. 부르지 않은 가사 줄은 줄과 구간을 확인할 것에 올린다.
`--repair-attempts 1–3`을 주면 그 구간만 다시 만들어 넣고, 다시 받아써서 고쳐진 것이 확인될 때만 그 결과를
쓴다(기본은 꺼짐). 생성 뒤 파일·음량·가사 받아쓰기·리듬과, 엔진이 실제로
적용했다고 보고한 템포·조성·박자를 검사해 확인할 구간을 표시한다. 리듬 경고는 청취 확인용이며 재생성이나
시간 보정의 근거로 쓰지 않는다. 자동 추천은 사람의 최종본 선택과 분리하고 새 음원은 `unreviewed`다.
정리본마다 −9 LUFS로 마스터링한 듣기용 사본을 따로 만들어 앱이 재생하고 내보낸다. 버전 음원은 그대로이며
`export --unmastered`로 받을 수 있다. 예전 곡은 `uv run music-engine master projects/my-song`으로 사본을 만든다.
검사 항목과 한계는 [자동 품질 검사](docs/AUTO-QUALITY.md)와 [타격 시각 검사](docs/TIMBRE-TRACKING.md)에 있다.

```bash
uv run music-engine generate projects/my-song --versions 3
uv run music-engine generate projects/my-song --seeds 101 --quality-attempts 2
uv run music-engine generate projects/my-song --quality audio
uv run music-engine generate projects/my-song --source-candidate-id <candidate-id> \
  --style "fuller chorus, clear lead vocal"
uv run music-engine revise projects/my-song --title "새 제목" --duration 120
uv run music-engine repaint projects/my-song --candidate-id <candidate-id> \
  --start 41 --end 58 --seed 104 --strength strong --model acestep-v15-xl-turbo
uv run music-engine resume projects/my-song --job-id <batch-job-id>
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

`repaint`는 고른 구간만 새로 만든 새 버전을 남기며 원본은 바꾸지 않는다. 새 버전의 바뀐 구간 밖은 부모
버전의 샘플 그대로이고, 그렇게 이어 붙이지 못한 경우는 결과의 `splice.reason`에 이유가 남는다
([구간 수정](docs/AUTO-QUALITY.md#구간-수정)). `analyze-rhythm`은 기존 WAV를 읽어 JSON으로 분석하며 음원이나 프로젝트를 바꾸지 않는다.
`analyze-rhythm --diagnostics`는 모델 없이 타격 성분을 추가 분석한다. 분리 결과가 없으면
반주 연속성은 판단 어려움으로 남는다. `inspect-rhythm`은 기존 후보에 새 분석 artifact·revision을
추가하며 원래 음원·기존 QC·사람 선택과 평가는 보존한다.

```bash
uv run music-engine analyze-rhythm path.wav --bpm 84 --diagnostics
uv run music-engine inspect-rhythm projects/my-song --candidate-id <candidate-id>
```

구간별 템포 변화나 반주 쉼이 의도된 것이면 그 음원 해시에 연결한 `--intent-json`으로 알려 대조할 수 있다.
형식은 [자동 품질 검사](docs/AUTO-QUALITY.md#구간별-의도-대조)에 있다. 보컬·반주·드럼 분리 근거를 더하려면
품질 환경의 UMXHQ를 쓴다(드럼 가중치 약 35.6 MB는 선택 준비). `separate`는 음원을 바꾸지 않는다.

```bash
.runtime/quality/.venv/bin/python scripts/quality_worker.py setup --drums
.runtime/quality/.venv/bin/python scripts/quality_worker.py separate --audio /absolute/path.wav > separated-energy.json
uv run music-engine analyze-rhythm /absolute/path.wav --diagnostics --separation-json separated-energy.json
uv run music-engine inspect-rhythm projects/my-song --candidate-id <candidate-id> --separation-json separated-energy.json
```

## 작업 서비스

앱 없이 `127.0.0.1:47833`에서 일을 받는 로컬 서비스다. arkAgent와 스크립트가 같은 대기열에 일을 맡기며
한 번에 하나만 실행한다. arkAgent의 엔진 계약(`arkAgent/docs/ENGINE-CONTRACT.md`) v0을 지킨다. 지금 받는 일은
모델 없이 몇 초 안에 끝나는 계약 점검 `selftest`뿐이고, 곡 생성 레시피는 아직 없다.

```bash
uv run music-engine serve
```

```bash
uv run --project ../arkAgent ark-engine-check http://127.0.0.1:47833
```

포트는 `--port`, 작업 기록을 둘 폴더는 `--output-dir`(기본 `output/`)로 바꾼다. 작업 기록은
`output/jobs/<작업 ID>/job.json`에 남고, 끝난 `selftest` 기록은 서비스가 시작할 때 지운다. 서비스의 작업과
CLI·앱의 생성(생성·구간 수정·커버·재개)은 동시에 돌지 않는다. 서비스가 일하는 동안 CLI·앱의 생성은 이유를
알리며 거절되고, CLI·앱이 생성하는 동안 서비스에 넣은 작업은 대기한다. CLI 생성끼리는 전처럼 함께 돈다.
손으로 켠 ACE·Music 3 서버가 기본 주소에 떠 있으면 작업이 없어도 `busy`이고 `resident: true`다. 쉬는 엔진은
모델 메모리를 쥐지 않는다는 계약을 어기는 상태라 숨기지 않고 알린다. 생성이 켠 ACE는 끝나면 스스로 꺼진다.
API와 기록은 [아키텍처](docs/ARCHITECTURE.md#작업-서비스)에 있다.

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

작업 서비스의 계약은 서비스를 띄운 뒤 위의 `ark-engine-check`로 확인한다. `selftest`만 쓰므로 모델을 올리지 않는다.

생성과 리듬 검사를 함께 실측하려면 기본 XL turbo 서버에서 새 30초 연주곡을 한 번 만든다.
원본·정리본의 분석과 artifact 해시, 사람 청취 `unreviewed` 상태를 확인한다.

```bash
./scripts/start_ace_api.sh
# 다른 터미널에서, 존재하지 않는 출력 디렉터리를 지정한다.
uv run python scripts/smoke_rhythm_quality.py --output-dir .runtime/rhythm-smoke-new
```

실측 실행기는 ACE를 직접 부르므로 손으로 켠 서버를 쓴다. 그 서버는 저절로 꺼지지 않으니 끝나면 끈다.
실제 추론은 모델 없는 계약·회귀 검사와 따로 실행한다. ACE의 다른 실측 실행기는
`scripts/smoke_real_engine.py`와 `scripts/compare_production.py`, Music 3를 다시 준비했을 때의 실행기는
`scripts/smoke_music3.py`와 `scripts/compare_music3_quality.py`다.

## 문서

- [현재 상태와 다음 작업](docs/HANDOFF.md)
- [아키텍처와 데이터 계약](docs/ARCHITECTURE.md)
- [자동 품질 검사](docs/AUTO-QUALITY.md), [타격 시각 검사](docs/TIMBRE-TRACKING.md)
- [사람 청취 계약](docs/QUALITY.md)
- [모델·라이선스 결정](docs/DECISIONS.md)
