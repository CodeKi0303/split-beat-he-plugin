# Split-Beat HE · Codex 데스크톱 플러그인

PC에 연결된 Split-Beat HE를 캘리브레이션하고 Rapid Trigger, 키 매핑, 센서와 타이밍을 확인합니다. 기존 Python MCP 서버를 로컬 USB HID로 실행합니다.

## 설치

1. `uvx`가 PATH에 있는지 확인합니다. 처음 시작할 때 Python 환경과 MCP 의존성을 준비하므로 인터넷 연결이 필요할 수 있습니다.
2. [GitHub 저장소](https://github.com/CodeKi0303/split-beat-he-plugin)의 설치 명령을 사용하거나, 배포 ZIP을 풀어 `plugin.json`이 있는 폴더를 로컬 Codex 마켓플레이스에 추가합니다. [Codex 로컬 플러그인 설치](https://developers.openai.com/plugins/build/plugins#install-a-local-plugin-manually).
3. 플러그인을 설치한 뒤 새 로컬 세션에서 “Split-Beat HE의 연결 상태와 키 설정을 확인해줘”라고 요청합니다. 처음에는 연결되지 않은 상태이며, USB 장치를 선택하면 읽기 및 요청한 설정 변경이 가능합니다.

WebHID의 키보드 연결을 종료한 다음 MCP에서 연결하세요. 설정 변경은 RAM에 먼저 적용되며, 영구 저장과 키 출력 변경은 명시적으로 요청할 수 있습니다. 펌웨어 파일은 MCP에서 검사할 수 있습니다. 설치는 [Studio](https://studio.codekilab.com/update.html)에서 대상 보드를 확인한 뒤 진행합니다.

서버만 설치하려면 배포 ZIP의 `server/`에 들어 있는 wheel을 Python 3.10 이상의 가상 환경에 설치합니다. 기본 실행은 읽기 전용입니다.

```text
python -m pip install path/to/split_beat_he_mcp-0.1.1-py3-none-any.whl
split-beat-he-mcp --help
```

MCP 클라이언트에서 `split-beat-he-mcp`를 stdio 명령으로 등록합니다. 설정 변경에는 `--write-enabled`를 추가합니다. 정식 펌웨어 배포 파일은 [GitHub Releases](https://github.com/CodeKi0303/split-beat-he-firmware/releases)에서 확인할 수 있습니다.

Claude Desktop은 [같은 릴리스의 `.mcpb`](https://github.com/CodeKi0303/split-beat-he-plugin/releases/tag/v0.1.1)를 Extensions 메뉴에서 설치합니다.
