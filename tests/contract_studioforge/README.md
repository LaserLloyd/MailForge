# StudioForge contract tests (MailForge)

`test_contract_studioforge.py` pins the exact OpenAI-compatible surface MailForge
uses when its LLM server is a StudioForge gateway, using the small JSON fixtures
in `fixtures/`.

**Provenance: every fixture is derived from the client code's expectations**
(what `src/mailforge/llm/bridge.py` sends and reads), not recorded from a live
server. If a StudioForge change breaks one of these shapes, the matching test
fails visibly in MailForge's CI, and the StudioForge change has to either be
reverted or land together with an updated fixture and client.

| Fixture | What MailForge depends on |
|---|---|
| `models.json` | `GET /v1/models -> data[].id`; additive fields are ignored |
| `chat_completion.json` | request keys incl. `priority: 2` and `X-SF-Client: mailforge`; reply in `choices[0].message.content` or, for reasoning models, `reasoning_content` |
| `embeddings.json` | request keys incl. `priority: 3`; reply `data[].{index,embedding}`; wider vectors are truncated to 768 |
| `errors.json` | `gpu_leased`, `priority_hold`, `insufficient_vram`, `model_not_found`, `unauthorized` all degrade to the sentinels (never raise) |

MailForge does **not** use StudioForge's management API (`/api/*`: leases,
load-recommended, status). A test asserts that, so a new dependency on it shows
up here first and the lease/load-recommended fixtures get added at that point
(see TransForge's and CrucibleForge's contract tests for those shapes).

Keep the fixtures small; add a field only when the client starts depending on it.
The tests use an in-process fake server on an ephemeral loopback port
(`tests/_fakesf.py`); the suite's socket guard (`tests/_netguard.py`) forbids any
other connection, so they can never reach a real StudioForge.
