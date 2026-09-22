# 현재 상태와 다음 작업

업데이트: 2026-09-22

## 완료

- P0: M4 Max에서 ACE-Step v0.1.8의 native MLX text-to-music와 repaint 실제 실행
- P1: 버전 있는 project/request/job/artifact/candidate/finding/revision 계약
- P1: 순차 후보, 개별 실패, SHA-256, PCM QC, 선택/되돌리기, WAV export, 재열기
- P1: 명시적 `resume`에서 완료 artifact 검증 후 재사용, 실패 seed 재실행
- P3 기반: CLI repaint 후보, 부모 계보, 선택 범위와 전체 문맥 범위 분리
- P2 일부: Electron 후보 카드, 파형, 동기 위치 전환, QC/청취 기록, 선택, 최근 프로젝트 복원

## 실행

```bash
uv sync --python 3.12.12 --group dev
./scripts/bootstrap_ace.sh
./scripts/start_ace_api.sh
npm install
npm run start:app
```

다른 터미널:

```bash
uv run music-engine init projects/my-song \
  --title "내 노래" --lyrics-file lyrics.txt \
  --style "Korean indie pop, clear vocal" --duration 120

uv run music-engine generate projects/my-song --seeds 101,102,103,104
uv run music-engine candidates projects/my-song
uv run music-engine review projects/my-song <candidate-id> --status listened --note "청취 메모"
uv run music-engine select projects/my-song <candidate-id>
uv run music-engine repaint projects/my-song --start 30 --end 42 \
  --instruction "Keep arrangement and improve Korean diction" --seed 201
uv run music-engine export projects/my-song --output exports/my-song.wav
uv run music-engine inspect projects/my-song
```

## 남은 작업

1. 사용자가 P0 세 후보와 repaint를 실제로 듣고 발음·음악성·연결부 판정을 남긴다.
2. 실제 2–3분 곡을 4 seeds로 생성해 시간·메모리·장곡 구조 문제를 측정한다.
3. 라이선스와 Mac 동작을 확인한 분리기·한국어 ASR을 채택하고 의심 구간 contract/UI를 추가한다.
4. Electron에 생성 진행과 앱 소유 런타임/중지 경로를 연결한다.
5. 청취 결과가 Turbo 한계를 보일 때만 SFT/XL 비교 다운로드와 실험량을 먼저 고지한다.

공식 REST API에는 개별 task 취소 endpoint가 없으므로 앱이 서버/작업자 프로세스 수명을
소유하기 전에는 `Ctrl-C` 후 원격 생성이 잠시 계속될 수 있다.
