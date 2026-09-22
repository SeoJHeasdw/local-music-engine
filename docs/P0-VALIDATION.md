# P0 Apple Silicon 실측

실측일: 2026-09-22, Asia/Seoul

## 환경

- MacBook Pro `Mac16,5`, Apple M4 Max, CPU 14 cores, unified memory 36GB
- macOS 26.7 (build 25G229)
- ACE 런타임이 추론 가능 메모리를 28.08GB로 감지
- 시작 전 disk 여유 498GiB
- CPython 3.12.12, PyTorch 2.10.0, MLX 0.30.6, mlx-lm 0.29.1
- ACE-Step v0.1.8, Turbo DiT + 0.6B LM, native MLX DiT/VAE/LM, CPU offload 없음

## text-to-music smoke

자작 검증 가사, 15초, 92 BPM, C Major, 4/4, seed `22092601`, Turbo 8 steps,
0.6B LM thinking을 사용했다.

| 항목 | 실측 |
| --- | --- |
| ACE 보고 생성 시간 | 총 9.88초, LM 1.52초, DiT+decode 8.36초 |
| 출력 | 48kHz, stereo, PCM s16le WAV |
| 실제 길이 | 720,000 frames, 정확히 15.000초 |
| 크기 | 2,880,078 bytes |
| peak | 0.8912658691 (-1 dBFS 정규화 결과) |
| channel RMS | 0.197943 / 0.189863 |
| near-digital-silence 비율 | 0.00000417 |
| SHA-256 | `96d696a2edd3ee80c807fa7045bcc15015c92062c70ffadc7ea73afbb694db1b` |

파일 무결성은 통과했다. 발음·가사·음악성은 사람 청취 전이므로 미확인이다.

## repaint smoke

위 결과의 5.0–9.0초를 seed `22092602`로 repaint했다. 공식 API 문서의 absolute path 방식은
서버가 `absolute audio file paths are not allowed`로 거부했고, 문서에 함께 나온 multipart
업로드 방식은 성공했다.

| 항목 | 실측 |
| --- | --- |
| ACE 보고 생성 시간 | 6.79초 |
| 출력 길이 | 원본과 같은 720,000 frames, 15.000초 |
| SHA-256 | `711ce2be97c53f579ea4094b94bc5fa04c911e3969eeeb198840584d48fe505b` |
| 평균 절대 차이 0–5초 | 0.017942 |
| 평균 절대 차이 5–9초 | 0.207889 |
| 평균 절대 차이 9–15초 | 0.010429 |

repaint는 선택 범위를 가장 크게 바꿨지만 범위 밖도 바꿨다. 길이 보존은 확인했으나 음악적
연결감과 가사 개선은 청취 미확인이다.

## 발견한 운영 문제

공식 시작 초기화의 사전 다운로드 경로와 handler의 `ACESTEP_CHECKPOINTS_DIR` 해석이 달라
통합 모델을 두 번 받으려 했다. 첫 다운로드 완료 후 서버를 중지하고 `.runtime/models`를
단일 정본으로 두며 ACE checkout의 `checkpoints`를 그 위치의 symlink로 만들었다. 이후
DiT/VAE/LM 로드는 성공했다. 초기 중복 시도의 불완전한 1.4GB 폴더는 완료 모델과 분리한 뒤
`~/.Trash/local-music-engine-models-partial-20260922-1106`으로 옮겼다. 완료 모델은 건드리지 않았다.

## CLI 통합

별도 `projects/p0-cli`에서 프로젝트 생성 → seed `22092603` 후보 생성 → 선택 → 내부/외부
WAV export → manifest 재열기와 모든 artifact 해시 검증 → 5–9초 repaint 후보 생성까지
수행했다. repaint는 새 후보로 남았고 자동 채택하지 않았다. 두 후보의 사람 검수 상태는
`unreviewed`다.

Electron 앱을 실제 실행해 최근 프로젝트 자동 복원, 두 후보 카드, 실제 WAV 파형, byte-range
재생, 같은 재생 위치 후보 전환, QC와 청취 상태 표시를 확인했다. renderer는 로컬 파일 경로를
받지 않는다.
