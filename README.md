# Split-Beat HE Plugin

codeki lab의 Split-Beat HE 키보드를 Codex에서 설정하고 진단하는 데스크톱 플러그인입니다. 같은 로컬 MCP 서버를 독립 설치해 다른 MCP 클라이언트에 등록할 수도 있습니다.

## Codex에서 설치

`uvx`가 PATH에 있어야 합니다. 첫 실행은 Python 의존성을 준비합니다.

```sh
codex plugin marketplace add CodeKi0303/split-beat-he-plugin --ref v0.1.0
codex plugin add split-beat-he-plugin@split-beat-he
```

설치한 뒤 Codex의 새 로컬 세션에서 **Split-Beat HE**를 선택합니다. “키보드 연결 상태와 설치된 펌웨어를 확인해줘”로 시작하세요. 초기 상태는 미연결이며, 장치를 선택하면 리비전과 실제 펌웨어의 지원 기능을 확인합니다. Studio에서 연결 중이면 Studio 연결을 먼저 종료합니다.

## 독립 MCP 설치

[GitHub Releases](https://github.com/CodeKi0303/split-beat-he-plugin/releases)에서 `split_beat_he_mcp-0.1.0-py3-none-any.whl`을 내려받아 Python 가상 환경에 설치합니다.

```sh
python -m pip install ./split_beat_he_mcp-0.1.0-py3-none-any.whl
split-beat-he-mcp --help
```

MCP 클라이언트에서 `split-beat-he-mcp`를 stdio 명령으로 등록합니다. 단독 실행은 읽기 전용으로 시작하며, 설정 변경에는 `--write-enabled`를 추가합니다. [MCP 사용 가이드](docs/MCP-CONTROL.md).

## 사용 범위

- 지원 기능은 설치된 펌웨어에서 읽습니다. 구버전은 그 버전에서 지원하는 설정과 진단을 사용할 수 있습니다.
- 플러그인에서 Hall 보정, Rapid Trigger, 키 매핑, 진단, 프로필, 설정 저장을 사용할 수 있습니다.
- 펌웨어 설치는 [Studio](https://studio.codekilab.com/update.html)에서 대상 보드를 확인해 진행합니다. 앱 펌웨어 릴리스는 [펌웨어 저장소](https://github.com/CodeKi0303/split-beat-he-firmware/releases)에서 관리합니다.

## 빌드

`python tools/package_mcp_plugin.py`가 ZIP과 wheel을 `dist/`에 만듭니다. 마켓플레이스에 번들되는 wheel도 새로 만들려면 `--marketplace`를 추가합니다. 서버는 펌웨어 저장소에서 검증한 로컬 MCP 구현을 기반으로 분리했습니다. [MIT](LICENSE).
