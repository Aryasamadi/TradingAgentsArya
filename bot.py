name: TradingAgents Engine + Telegram Controller

run-name: >-
  ${{ github.event_name == 'repository_dispatch'
      && format('Engine · {0}', github.event.client_payload.request_id)
      || 'Telegram Controller' }}

on:
  workflow_dispatch:
  repository_dispatch:
    types: [tradingagents_run]

permissions:
  contents: read
  actions: write

env:
  TRADINGAGENTS_OUTPUT_LANGUAGE: ${{ secrets.TRADINGAGENTS_OUTPUT_LANGUAGE || 'Persian' }}
  TRADINGAGENTS_CHECKPOINT_ENABLED: ${{ secrets.TRADINGAGENTS_CHECKPOINT_ENABLED || 'true' }}
  TRADINGAGENTS_DEFAULT_ANALYSTS: ${{ secrets.TRADINGAGENTS_DEFAULT_ANALYSTS || 'market,social,news,fundamentals' }}
  TRADINGAGENTS_MAX_DEBATE_ROUNDS: ${{ secrets.TRADINGAGENTS_MAX_DEBATE_ROUNDS || '1' }}
  TRADINGAGENTS_MAX_RISK_ROUNDS: ${{ secrets.TRADINGAGENTS_MAX_RISK_ROUNDS || '1' }}
  TRADINGAGENTS_MAX_TOOL_ROUNDS: ${{ secrets.TRADINGAGENTS_MAX_TOOL_ROUNDS || '20' }}
  TRADINGAGENTS_LLM_MAX_RETRIES: ${{ secrets.TRADINGAGENTS_LLM_MAX_RETRIES || '6' }}
  TRADINGAGENTS_MAX_TOKENS: ${{ secrets.TRADINGAGENTS_MAX_TOKENS || '8192' }}
  TRADINGAGENTS_TEMPERATURE: ${{ secrets.TRADINGAGENTS_TEMPERATURE || '0.0' }}
  TRADINGAGENTS_GOOGLE_THINKING_LEVEL: ${{ secrets.TRADINGAGENTS_GOOGLE_THINKING_LEVEL || 'medium' }}
  TRADINGAGENTS_OPENAI_REASONING_EFFORT: ${{ secrets.TRADINGAGENTS_OPENAI_REASONING_EFFORT || 'medium' }}
  TRADINGAGENTS_ANTHROPIC_EFFORT: ${{ secrets.TRADINGAGENTS_ANTHROPIC_EFFORT || 'high' }}

  TRADINGAGENTS_LLM_PROVIDER: ${{ secrets.TRADINGAGENTS_LLM_PROVIDER }}
  TRADINGAGENTS_DEEP_THINK_LLM: ${{ secrets.TRADINGAGENTS_DEEP_THINK_LLM }}
  TRADINGAGENTS_QUICK_THINK_LLM: ${{ secrets.TRADINGAGENTS_QUICK_THINK_LLM }}
  TRADINGAGENTS_LLM_BACKEND_URL: ${{ secrets.TRADINGAGENTS_LLM_BACKEND_URL }}

  OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
  AZURE_OPENAI_API_KEY: ${{ secrets.AZURE_OPENAI_API_KEY }}
  GOOGLE_API_KEY: ${{ secrets.GOOGLE_API_KEY }}
  ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
  XAI_API_KEY: ${{ secrets.XAI_API_KEY }}
  DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}
  DASHSCOPE_API_KEY: ${{ secrets.DASHSCOPE_API_KEY }}
  DASHSCOPE_CN_API_KEY: ${{ secrets.DASHSCOPE_CN_API_KEY }}
  ZHIPU_API_KEY: ${{ secrets.ZHIPU_API_KEY }}
  ZHIPU_CN_API_KEY: ${{ secrets.ZHIPU_CN_API_KEY }}
  MINIMAX_API_KEY: ${{ secrets.MINIMAX_API_KEY }}
  MINIMAX_CN_API_KEY: ${{ secrets.MINIMAX_CN_API_KEY }}
  OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}
  MISTRAL_API_KEY: ${{ secrets.MISTRAL_API_KEY }}
  MOONSHOT_API_KEY: ${{ secrets.MOONSHOT_API_KEY }}
  GROQ_API_KEY: ${{ secrets.GROQ_API_KEY }}
  NVIDIA_API_KEY: ${{ secrets.NVIDIA_API_KEY }}
  OPENAI_COMPATIBLE_API_KEY: ${{ secrets.OPENAI_COMPATIBLE_API_KEY }}

  AWS_BEARER_TOKEN_BEDROCK: ${{ secrets.AWS_BEARER_TOKEN_BEDROCK }}
  AWS_ACCESS_KEY_ID: ${{ secrets.AWS_ACCESS_KEY_ID }}
  AWS_SECRET_ACCESS_KEY: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
  AWS_SESSION_TOKEN: ${{ secrets.AWS_SESSION_TOKEN }}
  AWS_DEFAULT_REGION: ${{ secrets.AWS_DEFAULT_REGION }}
  AWS_PROFILE: ${{ secrets.AWS_PROFILE }}

  OLLAMA_BASE_URL: ${{ secrets.OLLAMA_BASE_URL }}
  TYPESAFE_API_KEY: ${{ secrets.TYPESAFE_API_KEY }}
  TYPESAFE_DEFAULT_MODEL: ${{ secrets.TYPESAFE_DEFAULT_MODEL }}
  FRED_API_KEY: ${{ secrets.FRED_API_KEY }}
  ALPHA_VANTAGE_API_KEY: ${{ secrets.ALPHA_VANTAGE_API_KEY }}
  SEC_EDGAR_USER_AGENT: ${{ secrets.SEC_EDGAR_USER_AGENT }}

jobs:
  controller:
    if: ${{ github.event_name == 'workflow_dispatch' }}
    runs-on: ubuntu-latest
    timeout-minutes: 350
    concurrency:
      group: tradingagents-telegram-controller-${{ github.repository }}
      cancel-in-progress: true
    steps:
      - name: Checkout repository
        uses: actions/checkout@v6

      - name: Restore controller state
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          set -euo pipefail
          mkdir -p .runtime/controller

          artifact_id="$(gh api "/repos/${GITHUB_REPOSITORY}/actions/artifacts?per_page=100" \
            --jq '[.artifacts[] | select(.name=="tradingagents-controller-state" and .expired==false)] | sort_by(.created_at) | reverse | .[0].id // empty')"

          if [[ -n "${artifact_id:-}" ]]; then
            gh api "/repos/${GITHUB_REPOSITORY}/actions/artifacts/${artifact_id}/zip" > .runtime/controller/state.zip
            unzip -o .runtime/controller/state.zip -d .runtime/controller >/dev/null
            rm -f .runtime/controller/state.zip
          fi

      - name: Install controller dependency
        run: python -m pip install --disable-pip-version-check PyNaCl

      - name: Start Telegram controller
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          BOT_GITHUB_TOKEN: ${{ secrets.BOT_GITHUB_TOKEN }}
          BOT_STATE_KEY: ${{ secrets.BOT_STATE_KEY }}
          TELEGRAM_ADMIN_IDS: ${{ secrets.TELEGRAM_ADMIN_IDS }}
          TELEGRAM_ALLOWED_USER_IDS: ${{ secrets.TELEGRAM_ALLOWED_USER_IDS }}
          BOT_STATE_PATH: ${{ github.workspace }}/.runtime/controller/bot_state.db
          GITHUB_OWNER: ${{ github.repository_owner }}
          GITHUB_REPO: ${{ github.event.repository.name }}
          GITHUB_REF: ${{ github.event.repository.default_branch }}
        run: python bot.py

      - name: Save controller state
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: tradingagents-controller-state
          path: .runtime/controller/bot_state.db
          retention-days: 90
          if-no-files-found: warn

  engine:
    if: ${{ github.event_name == 'repository_dispatch' }}
    runs-on: ubuntu-latest
    timeout-minutes: 360
    concurrency:
      group: tradingagents-engine-${{ github.repository }}
      cancel-in-progress: false
    steps:
      - name: Checkout repository
        uses: actions/checkout@v6

      - name: Set up Python
        uses: actions/setup-python@v6
        with:
          python-version: "3.13"
          cache: pip

      - name: Install TradingAgents
        run: python -m pip install -e .

      - name: Install request crypto dependency
        run: python -m pip install --disable-pip-version-check PyNaCl

      - name: Prepare runtime
        run: |
          set -euo pipefail
          mkdir -p .runtime/run .runtime/results .runtime/cache .runtime/cache/memory

      - name: Restore TradingAgents state
        env:
          GH_TOKEN: ${{ github.token }}
        run: |
          set -euo pipefail

          artifact_id="$(gh api "/repos/${GITHUB_REPOSITORY}/actions/artifacts?per_page=100" \
            --jq '[.artifacts[] | select(.name=="tradingagents-state" and .expired==false)] | sort_by(.created_at) | reverse | .[0].id // empty')"

          if [[ -n "${artifact_id:-}" ]]; then
            gh api "/repos/${GITHUB_REPOSITORY}/actions/artifacts/${artifact_id}/zip" > .runtime/state.zip
            unzip -o .runtime/state.zip -d .runtime/cache >/dev/null
            rm -f .runtime/state.zip
          fi

      - name: Read request
        env:
          REQUEST_JSON: ${{ toJSON(github.event.client_payload) }}
          BOT_STATE_KEY: ${{ secrets.BOT_STATE_KEY }}
        run: |
          set -euo pipefail
          python - <<'PY'
          import base64
          import hashlib
          import json
          import os

          payload = json.loads(os.environ.get("REQUEST_JSON") or "{}")
          if not payload.get("request_id"):
              raise SystemExit("Missing request_id")

          mode = str(payload.get("mode") or "analysis")
          params = payload.get("params") or {}

          def clean(value):
              return str(value if value is not None else "").replace("\n", " ").replace("\r", " ")

          def write_env(key, value):
              with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as f:
                  f.write(f"{key}={clean(value)}\n")

          write_env("REQUEST_ID", payload.get("request_id"))
          write_env("REQUEST_MODE", mode)
          write_env("REQUEST_CHAT_ID", payload.get("chat_id"))
          write_env("MODEL_ID", payload.get("model_id"))
          write_env("TICKER", params.get("ticker"))
          write_env("TICKERS", params.get("tickers"))
          write_env("ANALYSIS_DATE", params.get("date"))
          write_env("START_DATE", params.get("start"))
          write_env("END_DATE", params.get("end"))
          write_env("EVERY", params.get("every", "7"))
          write_env("ANALYSTS", params.get("analysts"))

          config_enc = str(payload.get("config_enc") or "")
          if config_enc:
              state_key = os.environ.get("BOT_STATE_KEY", "").strip()
              if not state_key:
                  print("Warning: config_enc received but BOT_STATE_KEY is not set. Falling back to global secrets.")
              else:
                  try:
                      from nacl.secret import SecretBox

                      key = hashlib.sha256(state_key.encode("utf-8")).digest()
                      decrypted = SecretBox(key).decrypt(base64.b64decode(config_enc))
                      cfg = json.loads(decrypted.decode("utf-8"))

                      token = str(cfg.get("api_token") or "")
                      if token:
                          print(f"::add-mask::{token}")
                          env_key = str(cfg.get("provider_env_key") or "")
                          if env_key:
                              write_env(env_key, token)

                      write_env("TRADINGAGENTS_LLM_PROVIDER", cfg.get("provider_type"))
                      write_env("TRADINGAGENTS_DEEP_THINK_LLM", cfg.get("model_name"))
                      write_env("TRADINGAGENTS_QUICK_THINK_LLM", cfg.get("model_name"))
                      write_env("TRADINGAGENTS_LLM_BACKEND_URL", cfg.get("base_url"))
                  except Exception as exc:
                      raise SystemExit(f"Failed to decrypt request model config: {exc}")
          PY

      - name: Validate unattended configuration
        run: |
          set -euo pipefail
          [[ -n "${TRADINGAGENTS_LLM_PROVIDER:-}" ]] || { echo "Missing active LLM provider."; exit 3; }
          [[ -n "${TRADINGAGENTS_DEEP_THINK_LLM:-}" ]] || { echo "Missing active deep-thinking model."; exit 3; }
          [[ -n "${TRADINGAGENTS_QUICK_THINK_LLM:-}" ]] || { echo "Missing active quick-thinking model."; exit 3; }
          [[ -n "${TRADINGAGENTS_LLM_BACKEND_URL:-}" ]] || { echo "Missing active model Base URL."; exit 3; }

      - name: Build agent command
        run: |
          set -euo pipefail
          mkdir -p .runtime/run

          if [[ "$REQUEST_MODE" == "analysis" ]]; then
            [[ -n "$TICKER" ]] || { echo "Missing ticker."; exit 2; }
            [[ -n "$ANALYSIS_DATE" ]] || { echo "Missing analysis date."; exit 2; }
            CMD=(tradingagents --ticker "$TICKER" --date "$ANALYSIS_DATE" --analysts "${ANALYSTS:-${TRADINGAGENTS_DEFAULT_ANALYSTS}}" --save --no-show --checkpoint)
          elif [[ "$REQUEST_MODE" == "backtest" ]]; then
            [[ -n "$TICKERS" ]] || { echo "Missing backtest tickers."; exit 2; }
            [[ -n "$START_DATE" && -n "$END_DATE" ]] || { echo "Missing backtest dates."; exit 2; }
            CMD=(tradingagents backtest "$TICKERS" --start "$START_DATE" --end "$END_DATE" --every "${EVERY:-7}" --analysts "${ANALYSTS:-${TRADINGAGENTS_DEFAULT_ANALYSTS}}")
          else
            echo "Unsupported mode: $REQUEST_MODE"
            exit 2
          fi

          printf '%q ' "${CMD[@]}" > .runtime/run/command.txt

          cat > .runtime/run/request.json <<EOF
          {
            "request_id": "${REQUEST_ID}",
            "mode": "${REQUEST_MODE}",
            "chat_id": "${REQUEST_CHAT_ID}",
            "ticker": "${TICKER}",
            "tickers": "${TICKERS}",
            "date": "${ANALYSIS_DATE}",
            "start": "${START_DATE}",
            "end": "${END_DATE}",
            "every": "${EVERY}",
            "analysts": "${ANALYSTS}"
          }
          EOF

          echo "Starting TradingAgents..."
          echo "Request ID: ${REQUEST_ID}"
          echo "Mode: ${REQUEST_MODE}"
          echo "Language: ${TRADINGAGENTS_OUTPUT_LANGUAGE}"

          "${CMD[@]}" 2>&1 | tee .runtime/run/agent.log

      - name: Build clean user report
        if: always()
        run: |
          set -uo pipefail
          mkdir -p .runtime/results

          python - <<'PY'
          import json
          import os
          import re

          home = os.path.expanduser("~/.tradingagents")
          out_path = ".runtime/results/report.md"
          os.makedirs(os.path.dirname(out_path), exist_ok=True)

          lines = []
          lines.append("# گزارش TradingAgents")
          lines.append("")
          lines.append(f"Request ID: `{os.environ.get('REQUEST_ID', '')}`")
          lines.append(f"Mode: `{os.environ.get('REQUEST_MODE', '')}`")
          lines.append(f"Language: `{os.environ.get('TRADINGAGENTS_OUTPUT_LANGUAGE', '')}`")
          lines.append(f"Ticker: `{os.environ.get('TICKER') or os.environ.get('TICKERS') or '—'}`")
          lines.append(f"Date/Range: `{os.environ.get('ANALYSIS_DATE') or (os.environ.get('START_DATE', '') + ' → ' + os.environ.get('END_DATE', '')) or '—'}`")
          lines.append("")

          extracted = []

          def clean_text(text):
              text = re.sub(r"\x1b\[[0-9;]*m", "", text or "")
              cleaned = []
              for line in text.splitlines():
                  s = line.strip()
                  if not s:
                      cleaned.append(line)
                      continue
                  if re.match(r"^(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b", s):
                      continue
                  if re.match(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}", s) and re.search(r"\b(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b", s):
                      continue
                  if s.startswith(("Traceback (", "    at ", "  File \"")):
                      continue
                  cleaned.append(line)
              return "\n".join(cleaned).strip()

          def add_section(title, body):
              body = (body or "").strip()
              if not body:
                  return
              extracted.append("")
              extracted.append(f"### {title}")
              extracted.append("")
              extracted.append(body)

          important_words = (
              "decision", "rating", "summary", "thesis", "target", "stop",
              "horizon", "recommendation", "action", "confidence", "reasoning",
              "plan", "portfolio", "final", "result", "report", "analysis",
          )
          skip_keys = {
              "run_settings", "tool_calls", "tool_call", "tool_response",
              "system_prompt", "prompt", "debug", "trace", "stack", "log",
              "checkpoint", "memory", "state", "metadata",
          }

          def extract_json(obj):
              if isinstance(obj, dict):
                  for k, v in obj.items():
                      lk = str(k).lower()
                      if lk in skip_keys:
                          continue

                      if isinstance(v, str):
                          if any(word in lk for word in important_words):
                              add_section(str(k), clean_text(v))
                      elif isinstance(v, (int, float, bool)):
                          if any(word in lk for word in important_words):
                              add_section(str(k), str(v))
                      elif isinstance(v, dict):
                          extract_json(v)
                      elif isinstance(v, list):
                          for item in v:
                              if isinstance(item, dict):
                                  extract_json(item)
              elif isinstance(obj, list):
                  for item in obj:
                      if isinstance(item, dict):
                          extract_json(item)

          text_files = []
          json_files = []

          if os.path.isdir(home):
              for root, dirs, files in os.walk(home):
                  dirs.sort()
                  files.sort()
                  for name in files:
                      path = os.path.join(root, name)
                      low = name.lower()
                      if low.endswith((".md", ".txt")):
                          if any(x in low for x in ("log", "debug", "trace", "checkpoint")):
                              continue
                          text_files.append(path)
                      elif low.endswith(".json"):
                          json_files.append(path)

          for path in text_files:
              try:
                  with open(path, "r", encoding="utf-8", errors="replace") as f:
                      body = f.read()
                  body = clean_text(body)
                  if body:
                      lines.append("")
                      lines.append("---")
                      lines.append("")
                      lines.append(f"## {os.path.basename(path)}")
                      lines.append("")
                      lines.append(body)
              except Exception:
                  pass

          for path in json_files[:30]:
              try:
                  with open(path, "r", encoding="utf-8", errors="replace") as f:
                      data = json.load(f)
                  extract_json(data)
              except Exception:
                  pass

          if extracted:
              lines.append("")
              lines.append("---")
              lines.append("")
              lines.append("## اطلاعات کلیدی استخراج‌شده")
              lines.extend(extracted)

          if len(lines) < 10:
              lines.append("")
              lines.append("---")
              lines.append("")
              lines.append("## وضعیت اجرا")
              lines.append("خروجی متنی قابل توجهی پیدا نشد یا اجرا ناموفق بود.")

          with open(out_path, "w", encoding="utf-8") as f:
              f.write("\n".join(lines) + "\n")

          print(f"Report written to {out_path}, size={os.path.getsize(out_path)}")
          PY

      - name: Save TradingAgents state
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: tradingagents-state
          path: .runtime/cache/
          retention-days: 90
          if-no-files-found: warn

      - name: Upload results
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: tradingagents-results-${{ github.run_id }}
          path: .runtime/results/
          retention-days: 90
          if-no-files-found: warn