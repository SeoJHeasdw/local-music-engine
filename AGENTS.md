# 저장소 작업 원칙

- Python 3.12를 사용한다. 프로젝트 환경은 루트 `.venv`, ACE-Step 환경은
  `.runtime/ace-step-1.5/.venv`로 분리한다.
- 모델, 생성 음원, 개인 입력, 작업 프로젝트, 로그는 Git에 넣지 않는다.
- `project.json`이 프로젝트·작업·artifact·선택 이력의 정본이다. 저장은 같은 디렉터리의
  임시 파일을 `fsync`한 뒤 원자적으로 교체한다.
- 완료 artifact를 재사용하려면 파일 존재, 크기, SHA-256이 모두 일치해야 한다.
  새 생성은 과거 결과를 묵시적으로 재사용하지 않는다.
- 원본 후보와 과거 revision은 덮어쓰지 않는다. repaint와 export는 새 artifact다.
- 자동 QC와 사람 청취 상태를 합치지 않는다. 자동 검사가 성공해도 `unreviewed`가 기본이다.
- renderer 또는 향후 앱에는 임의 파일 접근·명령 실행 권한을 노출하지 않는다.
- 기존 `local-tts-engine`은 설계 참고 대상일 뿐, 환경·모델·개인 데이터의 런타임 의존성이 아니다.

검사 명령:

```bash
uv sync --python 3.12.12 --group dev
uv run python -m compileall -q src tests
uv run pytest
npm run check:app
npm run test:app
```

실제 추론 검사는 별도로 ACE 서버를 띄운 뒤 수행한다. 실제 추론을 mock 검사로 대체했다고
기록하지 않는다.
