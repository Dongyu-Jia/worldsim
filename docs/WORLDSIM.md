# WorldSim: a small local world simulation

WorldSim does **world simulation only**: no pricing model and no charts. Asset prices, where a world has them, are
ordinary state fields that the synthesizer moves with the rest of the state.

```
world description (fixed) ─┐
input state ────────────────┼─► every actor answers its decision questions (+ autonomous actions), in parallel
(indicators, question       │                │
 statuses, narrative)       │                ▼
        ▲                   └─► synthesizer, step by step: digest → indicators (≤ x per call) → questions (≤ x)
        │                                                    → narrative (~100 字)
        └──────────────── output state: the same shape (one click = one step) ◄┘
```

- **World description**: a fixed text on the world (what it is, its scope, the setting, what one step means), shown
  to every actor and to the synthesizer in every iteration. The synthesizer never rewrites it.
- **State** = a value for every **indicator** (a typed field of the schema: number, percent, integer, enum, boolean,
  text), a **status** (现状描述, natural language) for every **predefined state question**, and a **narrative**: a
  summary of all values and statuses in about 100 字 (at most 120 characters; about 80 words, at most 600 characters,
  in a non-CJK world). The state questions are fixed and configurable like the fields; the synthesizer rewrites every
  status in every iteration.
- **Actors** have a role, what they care about, private information only they see, the indicators they can see and an
  optional model, plus:
  - **decision questions**: a short list (typically 1–4) of questions the actor answers **in text** every iteration,
    each with optional guidance (what a good answer covers: units, typical options, magnitudes) and a required flag;
  - **autonomous actions**: actions it decides on its own beyond the questions, 0 up to a maximum (default 3), each
    with a title, detail, target and intended effect, with optional guidance on what fits the actor. On by default;
    can be switched off per actor.
- The **synthesizer** is multi-shot: every iteration it makes a digest of the period (本季要事), then writes the
  indicators in chunks of at most **x** (default 6), then the question statuses in chunks of at most x, then the
  narrative. Each call answers only its own keys, so each item gets the model's full attention.
- LLM calls go to **OpenRouter** with your key (default model `google/gemini-4-argon`), or to a deterministic **mock**
  that needs no key.

## Run it

```bash
pip install -e ".[dev]"              # once (fastapi, uvicorn, requests; pytest, httpx, ruff for development)
python -m worldsim                   # http://127.0.0.1:8940
WORLDSIM_PORT=8950 python -m worldsim
WORLDSIM_LLM=mock python -m worldsim # force the free mock backend (no key, no network)
```

Open http://127.0.0.1:8940. On the first start the server creates the preloaded world 「美国与世界 · 2026Q4」.
Click **Run one iteration** on the Simulation page: each click is one step (one quarter in the default world).

| Environment variable | Default | What it does |
|---|---|---|
| `WORLDSIM_PORT` | 8940 | the port (the server always binds 127.0.0.1) |
| `WORLDSIM_DATA_DIR` | `data/` in a source checkout (git-ignored), else `~/.worldsim` | where worlds, settings and the model-list cache live |
| `WORLDSIM_LLM` | — | `mock` or `openrouter`: overrides the backend chosen in Settings |
| `WORLDSIM_OFFLINE` | — | `1`: never fetch the public model list (only the default model is listed) |
| `WORLDSIM_MOCK_DELAY_S` | 0.3 | how long each mock call takes, so the live progress is visible |
| `WORLDSIM_MOCK_SCRIPT` | — | scripted mock behaviour to try the failure paths, e.g. `{"synth:values:2": ["error", "error"]}` (that indicator step fails twice and is carried forward). Keys: `actor:<id>`; `synth:<step>` (`synth:digest`, `synth:values:3`, `synth:statuses:1`, `synth:narrative`), `synth:<kind>` (any step of a kind), `synth` (any step); add `:repair` for repair calls. Behaviours: `ok`, `error`, `timeout`, `auth` (a refused key: the iteration pauses), `bad_json`, `invalid` (an actor leaves its required questions unanswered; a synthesizer step answers `{}`), `long` (a narrative over the limit), `extra` (an indicator step adds a key outside its chunk) |

## Where to put the OpenRouter key

Open **Settings → OpenRouter API key**, paste the key (`sk-or-v1-…`, from https://openrouter.ai/keys) and click
**Save key**; **Test key** calls `GET https://openrouter.ai/api/v1/key` and shows the usage and limit.

- The key is stored only in `settings.json` in the data folder (git-ignored), written with file mode **0600**.
- The browser never gets it back: Settings shows it masked (`sk-or-…abcd`). It can be replaced or cleared.
- It is sent only to `openrouter.ai` in the `Authorization` header. It is never written to logs, error messages,
  prompts, saved iterations or exports (errors are redacted; a test checks every saved file).
- WorldSim does not read `.env` or an environment variable for the key.
- Without a key, choose **Mock (no key)** in Settings (or start with `WORLDSIM_LLM=mock`) to try everything for free.

## Models and cost

- **Default model**: `google/gemini-4-argon` (in OpenRouter's public model list on 2026-11-02: $2 / M input tokens,
  $10 / M output tokens, 1,048,576 context, structured outputs, reasoning always on with efforts low / medium / high).
- **Model picker**: the public list (`GET https://openrouter.ai/api/v1/models`, no key, cached for a day in
  `models_cache.json` in the data folder) with search; any model id can also be typed. Overrides: per world (Worlds page),
  per actor (Actors page) and for the synthesizer (Synthesizer page).
- **Settings**: temperature, reasoning effort (blank = the model's default), max parallel calls (default 6), timeout
  per call (default 240 s), max tokens (blank = provider default), and *Record the model's thinking* (default on):
  for a model whose `supported_parameters` include `reasoning`, every request carries `reasoning: {exclude: false}`
  (plus the effort when one is set), so the reasoning comes back and is saved with the call. It costs nothing extra:
  reasoning tokens are billed whether or not they are returned. A model without the parameter never gets it.
- **Requests**: `POST /api/v1/chat/completions` with `HTTP-Referer: http://localhost`, `X-Title: WorldSim`,
  `usage: {include: true}`, and `response_format: {type: "json_schema", strict: true}` when the model lists structured
  outputs (with `provider.require_parameters`), else `{type: "json_object"}`; if a provider refuses the JSON schema
  (HTTP 400/404/422) the call is repeated once in JSON mode. Every answer also goes through a tolerant JSON parser.
- **Cost**: the Simulation page shows an estimate before each click (the real prompts' length, an assumed answer
  length and a reasoning allowance, priced with the model list) and the actual cost after it (OpenRouter's
  `usage.cost`; computed from the list prices when absent). The default world's estimate with Gemini 4 Argon is about
  **$2.1–2.4 per iteration**: 36 calls (14 actors + 22 synthesizer steps at x = 6), most of it the synthesizer's
  prompts (each step sees the previous state, every decision and what was written before it) and the model's
  mandatory reasoning. A larger x means fewer, longer synthesizer calls; the number of calls is shown next to the
  **Run** button. Mock runs are free.

## An iteration, step by step

1. Every **enabled actor** is called in parallel (at most *max parallel* at once). Its prompt holds the world
   description, its role, what it cares about, its decision questions with their guidance, how many autonomous
   actions it may take and what kind, its private information, the current state (its visible indicators, every
   question's status, the narrative) and the last *history window* steps (summaries, digests, narratives, everyone's
   public answers, actions and statements; never anyone's rationales). It answers one JSON object:
   `{answers: {question id: {value, rationale}}, actions: [{title, detail, target, intended_effect}],
   public_statement, rationale, expected_consequences[]}` (`actions` only when autonomy is on). Validation: every
   required question needs a non-empty text answer (an optional one may be empty); actions beyond the maximum are
   dropped, and actions from an actor whose autonomy is off are ignored (both noted).
2. The **synthesizer** runs its steps one after another (`model.synth_plan`):
   - **digest** (本季要事): `{summary, digest, threads[]}`: about 300 字 on the main events, how the decisions
     interact or conflict and which forces dominate, plus a few causal threads;
   - **indicator chunks**: the fields split into chunks of at most x in schema-group order (a group stays together
     when it fits, a group larger than x is split into balanced pieces, small neighbouring groups are merged):
     `{values: {field id: {value, reason}}}`;
   - **question chunks**: the state questions in chunks of at most x, the same way, after every number is settled:
     `{statuses: {question id: {status, changed, reason}}}` (a status is restated in full when it did not change);
   - **narrative**: `{narrative}`, the ~100-字 summary of all new values and statuses.

   Every step sees the world description, the synthesizer's instructions, the full previous state, a short history,
   every actor's decisions (answers with question text and rationale, autonomous actions, statements, and which
   required questions were left unanswered) and everything written earlier in the iteration. Its structured-output
   schema holds only its own keys; keys outside the step are dropped (and flagged).
3. **Validation** of the merged answer against the schema: numbers / percents / integers / booleans / enums are coerced
   when the text is readable (noted), else the previous value is carried forward and flagged; a missing or null
   field, and a missing or empty status, is carried forward and flagged; a value outside the plausible range is kept
   and flagged; a changed value or status without a reason is flagged; a narrative over the limit is cut and flagged.
   Nothing is invented.
4. The new state is saved as the next step, with the iteration record: every actor's output, the digest, every
   synthesizer step (its items, status, attempts, output), the flags, the value and status changes, and **every
   call's prompt, raw response, token usage and cost** (Simulation → expand a synthesizer step, or *Show all prompts &
   raw responses*).

**Failures.** An actor call that fails is retried once, then the actor is marked *no response* and the synthesizer
is told to treat it as the status quo. An unusable answer (bad JSON, or a required question without an answer) gets
one repair call first; a required question still unanswered after the repair is kept, marked **unanswered** (shown in
the step viewer) and the synthesizer is told not to invent an answer. A synthesizer step that fails is retried once
(each attempt with one repair); a step that still fails carries its items forward from the previous step with the
reason 「合成失败，沿用上期」 and an error flag, and the iteration is saved but marked **partial**. Only when every
indicator and question step fails does the iteration fail: the state is unchanged, the error is shown with a **Retry synthesizer** button (the actors'
outputs are kept, so only the synthesizer runs again) or **Discard**.

**Checkpoints (resume after a crash or a pause).** The iteration in flight is saved in `checkpoint.json` after every
actor that answers and every synthesizer step that finishes. If the server stops mid-iteration (a restart, a crash,
the app closing it), nothing that finished is lost: the Simulation page shows *The iteration … was interrupted* with
what was saved and the cost so far, and **Resume** calls only what is left (the answered actors and finished steps are
reused as they were, never paid for again). A refused key or exhausted credits (HTTP 401 / 402 / 403) **pauses** the
iteration at once instead of marking actors *no response* or carrying steps forward: fix the key or top up, then
Resume. Any other single failure stays local: an actor without an answer after its retry is *no response*, a step
that fails is carried forward, and the iteration goes on. A checkpoint resumes only into the same world and step: after
an edit of the world or a rewind it is refused (Discard it and run a new iteration); *Audit what was done* shows its
calls on the Audit page.

**History.** Every step can be viewed with the iteration that produced it. **Rewind** to a step drops the later steps
(after a confirmation); **Fork** creates a new world with the history up to that step.

## The pages

- **Simulation**: the world description (collapsible); *Run one iteration* with the number of calls per iteration,
  the cost estimate and the last cost; live progress per actor (with the gist of its first answer) and per
  synthesizer step (Digest, Indicators 2/14, Questions 1/6, Narrative); the step history; and for the selected step:
  the narrative, the indicator table (value, change, reason, flags), the state questions with their statuses (changed
  ones marked, with the reason and the previous status), and the iteration: each actor's answers (question → answer
  → rationale; unanswered ones marked) and autonomous actions as cards, every synthesizer step with its output and
  its input and raw output on demand, and the lists of value and status changes. Rewind and fork. Every actor card
  and synthesizer step links to its audit.
- **Audit**: one step (or the failed attempt awaiting retry) at a time; the participants (the actors, then the
  synthesizer steps) with status, number of calls and whether thinking was recorded; for the chosen one, its final
  accepted output and then every call in order (first call, repair, retry round), each in three parts: **Input**
  (the messages exactly as sent with their roles, the request settings, the answer's JSON schema and the exact request
  body; the key is only in a header and never recorded), **Thinking** (the reasoning exactly as the provider returned
  it in `message.reasoning` or `reasoning_details`, with the reasoning tokens; otherwise a note says why there is none:
  the model returned no readable thinking, only encrypted blocks, capture is off, the model takes no reasoning
  parameter, or the call was saved before thinking capture existed; the mock's text is marked as mock) and
  **Output** (the raw response text, the JSON parsed from it, accepted or why not, notes, the finish reason, the raw
  response JSON and, after a refused schema, both HTTP requests). *Older* / *Newer* keep the participant, to follow
  one actor across steps. Links: `#audit?step=3&who=fed`, `#audit?step=pending`. What "thinking" is depends on the
  provider: Gemini returns thought summaries, not its raw chain of thought.
- **Actors**: list and editor (name, id, enabled, role, cares, **decision questions** with add / edit / delete /
  reorder, question text, guidance and required, **autonomous actions** on / off with the maximum and guidance,
  private info, visible fields, model override).
- **Schema**: the **world description**; the indicator fields (add, edit, delete, reorder, group, type, unit,
  description, options, plausible min/max, historical-range note, tag) with their initial values; the **predefined
  state questions** (add, edit, delete, reorder, id, text, group, guidance) with their initial status; the initial
  period and narrative (with a character count against the limit). The initial state is editable while the world has
  no later steps (rewind to step 0 or fork otherwise); fields and questions can be added at any time (the synthesizer
  fills a new one).
- **Synthesizer**: instructions, **items per call (x, 1–30)** with the resulting number of calls, model override.
- **Worlds**: the current world's name, output language, period step (quarter / month / year × n), history window
  and model settings; the list (open, duplicate, export JSON, delete with confirmation); create blank or from the
  template; import a JSON export (history included).
- **Settings**: backend, OpenRouter key, default model, call parameters, the model list.

The UI is plain ES modules (`worldsim/web/`), no build step, English chrome with the world's own text in its language.
All server and model text is inserted as text, never as HTML, under a strict Content-Security-Policy. It works at
1,100 px and at 375 px.

## Storage

`<data>` is `data/` in a source checkout, `~/.worldsim` for an installed package, or `$WORLDSIM_DATA_DIR`.

```
<data>/settings.json                         backend, key (0600), default model, call parameters
<data>/models_cache.json                     the public model list (refreshed daily)
<data>/worlds/<id>/world.json                configuration: description, schema (fields, questions), actors,
                                             synthesizer, initial state
<data>/worlds/<id>/steps/000000.json         the state at each step (step 0 = the initial state)
<data>/worlds/<id>/iterations/000001.json     how step n was produced (outputs, digest, synthesizer steps,
                                             flags, usage, cost) and every call: messages, request body (without
                                             its messages), thinking, raw response, parsed JSON, verdict, HTTP
                                             attempts (each text capped at 256 KB, a cut copy marked so)
<data>/worlds/<id>/pending.json              a failed synthesizer attempt awaiting retry
<data>/worlds/<id>/checkpoint.json           the iteration in flight (answered actors, finished steps, calls): resumed
                                             after a restart or a pause
<data>/worlds/<id>/pre_v2/                   world.json and steps/ as they were before a migration (below)
```

**Migration of worlds saved in the first format** (`worldsim-world/1`). They are converted the first time they are
loaded, and the files as they were are copied to `pre_v2/` first. Each fixed action space becomes one required
decision question 「本季你会怎么做？」 (「本月」/「今年」 for other period units; "What will you do this quarter?" in
English) with the former actions (status quo first, with their descriptions) as its guidance, and autonomous actions
switched on. The free-floating key questions and attributes become predefined state questions (groups 关键问题 and
属性, in order of first appearance across the saved steps), each saved step's own text becoming its status there
(「未定。当前判断：…」, 「已有答案：…」, the attribute's value, or 「（这一步没有记录）」); the old narrative is kept.
Old iteration records are left as they were and shown in their old layout; an old actor output in the history or in
a pending retry is shown to the models as "action: … [id]". Importing a first-format export (with its history) is
migrated the same way. No saved world breaks.

## Security

The server binds `127.0.0.1`. Every `/api` request must carry a local `Host` header (no DNS rebinding); every
state-changing request (POST / PUT / PATCH / DELETE) must come from the app's own pages (`Origin` /
`Sec-Fetch-Site`) and carry a JSON body. Deleting a world and rewinding need
`{"confirm": true}`.

## The default world: 「美国与世界 · 2026Q4」

Built from the local data of [Causeway](https://github.com/Dongyu-Jia/causeway) by its
`tools/build_worldsim_default.py` and shipped here as `worldsim/defaults/us_2026q4.json`, so WorldSim needs none of
that data. The `data/…` paths below are in Causeway's repository; refresh the file by re-running the builder there.

- **World description**: four short paragraphs: a US-centred political-economy world starting at the beginning of
  2026Q4, one quarter per step; its scope; the fourteen actors and what they do each quarter; what the state holds
  (indicators from Causeway's data, the state questions' statuses, a ~100-字 summary; asset prices as ordinary fields,
  no pricing model).
- **Indicators** (62): the state at the start of 2026Q4. The panels (`data/processed/panel_monthly.csv`,
  `panel_quarterly.csv`) end 2026-09; raw FRED series fill payrolls, housing starts, the mortgage rate, the breakeven
  and wages; the actors' books (`data/results/agents_accounts_root.json`) give the EFFR, the Fed's balance sheet, the
  fiscal lines, gasoline, the SPR and the yuan (their dates are in each field's description). Each numeric field has
  its historical range (from the same series) and plausible bounds (the historical range widened by a quarter of its
  span; judgment bounds where no local history exists, marked so).
- **Asset prices** (group 资产价格, eight fields; the 10-year yield, WTI and the dollar index sit in their own groups):
  the S&P 500 (Shiller's monthly S&P composite, September average 7,669.4, from `data/raw/shiller/ie_data.xls`, the
  file Causeway's CAPE comes from), the Nasdaq-100 (FRED NASDAQ100, end of September 30,408.5), **QQQ** 739.61 and
  **TLT** 76.01 (Causeway's start prices in `data/results/instrument_meta.json`: 13F prices of 2026-06-30 rolled to
  September 30 by the index return and a duration model, so tagged 估算), gold (London monthly average, September
  4,319 USD/oz, `data/raw/gold/monthly.csv`), the VIX (FRED VIXCLS, 16.3 on September 30), the Case-Shiller national
  index (FRED CSUSHPISA, August 334.2) and the FHFA purchase-only index (FRED HPIPONM226S, August 444.6). Each
  description cites its source and date and the later reading where one exists. Not available locally and so left
  out rather than invented: the median existing-home price (NAR) and Bitcoin.
- **The numbers quoted when the world was specified all match the data**: 10-year TIPS 2.93%, debt held by the
  public / GDP 97.37%, consumer sentiment 48.1 (this is the University of Michigan index, not the Conference
  Board's), initial claims 200.5k (September monthly average), labour share index 93.4, after-tax profits / GDP
  13.14%, dividend yield 1.03%, IT investment / GDP 5.05%. Noted in descriptions: the panel's effective tariff rate
  8.22% is 2026Q2, the start value 6.85% is the NIPA 2026Q3 rate (refunds of $134.4bn since May, cause unconfirmed);
  the 10-year yield was 5.29% at the end of September and 4.98% at the end of October.
- **Qualitative fields** come from the pinned facts in `data/agents/institutions.json`; every one still unconfirmed
  there is tagged 未确认 (two judgment fields are tagged 判断).
- **Predefined state questions** (22, in six groups), each with guidance and a status of two to four sentences
  written from the data and the pinned facts (未确认 where a fact is unconfirmed, 判断 where it is a judgment):

| Group | State question (`id`) |
|---|---|
| 货币与金融 | 美联储的政策立场与独立性如何？ (`fed_policy`) |
|  | 通胀的成因与动态如何？ (`inflation`) |
|  | 金融条件与信用状况如何？ (`financial_conditions`) |
|  | 银行体系是否稳健？ (`banks`) |
| 财政与国内政治 | 财政立场与赤字路径如何？ (`fiscal`) |
|  | 债务上限与 X 日期的局势如何？ (`debt_limit`) |
|  | 中期选举与国内政治格局如何？ (`politics`) |
|  | 关键诉讼（IEEPA 关税、美联储理事免职、反垄断）进展如何？ (`courts`) |
| 贸易与地缘政治 | 关税制度与其法律状态如何？ (`tariffs`) |
|  | 中美关系（贸易休战、出口管制、台海）如何？ (`us_china`) |
|  | 欧洲与全球增长如何？ (`global_growth`) |
|  | 美元与资本流动如何？ (`dollar_flows`) |
| 能源与冲突 | 石油供给中断与冲突的态势如何？ (`oil_conflict`) |
|  | 欧佩克+ 的产量政策如何？ (`opec`) |
|  | 美国的能源供应与油价负担如何？ (`energy_supply`) |
| 经济与企业 | 劳动力市场的状况如何？ (`labor`) |
|  | 消费者与居民的状况如何？ (`consumers`) |
|  | 美国经济增长的势头与衰退风险如何？ (`growth`) |
|  | AI 资本开支周期处在什么阶段？ (`ai_capex`) |
|  | 企业盈利与利润率如何？ (`profits`) |
|  | 房地产市场的状况如何？ (`housing`) |
| 市场与资产 | 资产市场的状况与估值如何？ (`markets`) |

- **Narrative** (103 characters): 「2026Q4 季初：通胀 3.7% 偏高、就业放缓（失业率 4.2%），美联储加息后观望；冲突使油价约 97
  美元，财政扩张、债务上限临近；AI 投资高涨、股市估值处于高位；中期选举与中美休战到期悬而未决。」
- **Actors**: fourteen. Their decision questions follow each actor's levers in Causeway's catalogue
  (`scenariolab/agents/actors.py`); the guidance of each question carries the
  constraints in words (e.g. the Fed's rate question: 「以 25bp 为单位给出本季累计变动……通常在 −100bp 到 +100bp
  之间」; OPEC+: 「给出相对当前配额的变化（例如 0、+41、−100 万桶/日）与生效月份」). Every actor has autonomous
  actions on with guidance (「例如：……」); the courts and the voters may take at most two, the others three. Private
  information is each actor's book (balance sheet, income, cash flow highlights) plus the pinned facts that concern it.

| Actor (`id`) | Decision questions (* = optional) | Autonomous actions |
|---|---|---|
| 美联储 (`fed`) | 本季政策利率怎么调、调多少？ · 资产负债表怎么安排？ · 前瞻指引的基调是什么？ | ≤ 3 |
| 总统与白宫 (`president`) | 本季关税政策怎么调整？ · 对中国的谈判与出口管制立场是什么？ · 是否动用战略石油储备、动用多少？ · 对美联储采取什么姿态？ * | ≤ 3 |
| 国会 (`congress`) | 本季财政立法的方向与规模是什么？ · 债务上限怎么处理？ · 拨款到期时是否发生政府停摆？ | ≤ 3 |
| 财政部 (`treasury`) | 本季发行结构怎么调整？ · 如何应对债务上限？ · 国债回购的规模怎么安排？ * | ≤ 3 |
| 最高法院与法院 (`courts`) | 本季是否就 IEEPA 关税授权作出裁决？结果是什么？ · 关于总统罢免美联储理事的权力，本季有无裁决？ · 对大型科技公司的反垄断案有无裁决或救济？ * | ≤ 2 |
| 选民 (`voters`) | 中期选举的结果如何？ · 你们对总统与执政党的支持怎么变化？ · 现在最在意的两三个议题是什么？参与政治的热情怎样？ * | ≤ 2 |
| 中国 (`china`) | 对美国的关税与出口管制，本季如何回应？ · 人民币汇率如何管理？ · 国内宏观刺激的力度如何？ · 台海方面本季的动作是什么？ * | ≤ 3 |
| 欧佩克+ (`opec`) | 本季产量配额变化多少（万桶/日）？ · 如何处理配额纪律与沙特的角色？ * | ≤ 3 |
| 冲突方 (`belligerents`) | 本季冲突走向如何：僵持、升级还是停火？ · 石油供给中断的程度怎么变化（万桶/日）？ | ≤ 3 |
| 超大型科技公司 CEO (`bigtech`) | 未来四个季度的 AI 资本开支指引怎么调整？ · 招聘与用工怎么安排？ · 股东回报（回购、股息）与 AI 定价怎么调整？ * | ≤ 3 |
| 消费者/居民 (`households`) | 本季支出与储蓄怎么平衡？ · 买房意愿如何？ · 借贷还是去杠杆？ * | ≤ 3 |
| 华尔街投资者 (`investors`) | 风险偏好怎么变化？ · 股债配置怎么调整？ · 科技股仓位怎么调整？ | ≤ 3 |
| 银行/放贷机构 (`banks`) | 本季放贷标准怎么调整？ · 存款定价与证券组合怎么处理？ * | ≤ 3 |
| 非科技企业 (`firms`) | 本季招聘计划如何？ · 资本开支怎么调整？ · 定价怎么调整：转嫁成本还是让价保份额？ | ≤ 3 |

- **Synthesizer instructions** (Chinese): resolve all decisions jointly; work step by step and stay consistent with
  what earlier steps wrote; keep fields consistent with each other and with the decisions; change only what the
  quarter's events move; a reason for every change; plausibility against history; enum options exactly; **move asset
  prices consistently with the quarter's events and the other fields (rates and the term premium, earnings and AI
  investment, risk appetite and credit spreads, the dollar, inflation and safe-haven demand), with a reason, staying
  plausible against their history** (QQQ moves with the Nasdaq-100, TLT against long yields); restate every
  question's status (2–4 sentences, honest about uncertainty, unconfirmed facts still marked); a narrative of about
  100 字; actors with no response keep the status quo, and unanswered questions are not answered for them. x = 6:
  22 synthesizer steps (1 digest, 14 indicator chunks, 6 question chunks, 1 narrative).

| Group | Field (`id`) | Start value | Unit | Plausible range | Tag |
|---|---|---|---|---|---|
| 政策利率与收益率曲线 | 联邦基金利率（有效） (`policy_rate`) | 3.88 | % | 0 … 24 |  |
|  | 美联储总资产 (`fed_assets`) | 6745.6 | 十亿美元 | 0 … 11000 |  |
|  | 2 年期国债收益率 (`y2`) | 4.88 | % | 0 … 21 |  |
|  | 10 年期国债收益率 (`y10`) | 5.29 | % | 0 … 20 |  |
|  | 10 年期 TIPS 实际收益率 (`tips10`) | 2.93 | % | -2.3 … 4.3 |  |
|  | 10 年期限溢价（ACM） (`term_premium`) | 0.89 | 百分点 | -3.1 … 6.9 |  |
| 物价与通胀预期 | CPI 同比 (`cpi_yoy`) | 3.72 | % 同比 | -8 … 19 |  |
|  | 核心 CPI 同比 (`core_cpi_yoy`) | 2.57 | % 同比 | -3 … 17 |  |
|  | 核心 PCE 同比 (`core_pce_yoy`) | 3.12 | % 同比 | -2 … 12 |  |
|  | 1 年期通胀预期（密歇根） (`infl_exp_1y`) | 4.6 | % | -3 … 13 |  |
|  | 长期通胀预期（密歇根 5–10 年） (`infl_exp_lr`) | 3.4 | % | 1.5 … 5.4 |  |
|  | 10 年期盈亏平衡通胀率 (`breakeven10`) | 2.35 | % | -0.5 … 3.6 |  |
| 增长与投资 | 实际 GDP 增长（季度年化） (`real_gdp_growth`) | 3.3 | % 年化 | -49 … 46 |  |
|  | IT 设备与软件投资/GDP (`it_invest_gdp`) | 5.05 | % GDP | 0 … 6.2 |  |
|  | 超大型科技公司资本开支（近 4 季合计） (`hyperscaler_capex_ttm`) | 691.0 | 十亿美元 | 0 … 2000 |  |
| 劳动力市场 | 失业率 (`unemployment`) | 4.2 | % | 0 … 18 |  |
|  | 非农就业月均新增（近 3 个月） (`payrolls_3m`) | 50.7 | 千人/月 | -9800 … 5500 |  |
|  | 首次申领失业救济（月均） (`initial_claims`) | 200.5 | 千人/周 | 0 … 5800 |  |
|  | 时薪同比（生产与非管理人员） (`wage_growth`) | 3.3 | % 同比 | -1 … 12 |  |
|  | 劳动收入占比（指数，2017=100） (`labor_share`) | 93.4 | 指数 | 80 … 130 |  |
| 信用与金融条件 | Baa 信用利差（相对 10 年国债） (`baa_spread`) | 1.47 | 百分点 | 0 … 7.6 |  |
|  | GZ 信用利差 (`gz_spread`) | 0.93 | 百分点 | 0 … 9.7 |  |
|  | 银行放贷标准（SLOOS 净收紧比例） (`lending_std`) | 0.0 | % 净收紧 | -100 … 100 |  |
| 财政 | 联邦赤字/GDP (`deficit_gdp`) | 5.22 | % GDP | -12 … 35 |  |
|  | 联邦公众持有债务/GDP (`debt_gdp`) | 97.37 | % GDP | 0 … 130 |  |
|  | 净利息支出/联邦收入 (`net_interest_share`) | 19.46 | % | 0 … 50 |  |
|  | 债务上限余量 (`debt_limit_headroom`) | 713.2 | 十亿美元 | 0 … 10000 |  |
| 住房 | 新屋开工（年化） (`housing_starts`) | 1221.0 | 千套 | 0 … 3000 |  |
|  | 房价同比（Freddie Mac） (`hpi_yoy`) | 2.19 | % 同比 | -21 … 27 |  |
|  | 30 年期抵押贷款利率 (`mortgage30`) | 7.03 | % | 0 … 23 |  |
| 企业与收入分配 | 企业税后利润/GDP (`profit_share`) | 13.14 | % GDP | 1 … 16 |  |
|  | 美股股息率 (`dividend_yield`) | 1.03 | % | 0 … 8.7 |  |
| 资产价格 | 标普 500 指数 (`spx`) | 7669.4 | 点 | 0 … 9700 |  |
|  | 纳斯达克 100 指数 (`ndx`) | 30408.5 | 点 | 0 … 38000 |  |
|  | QQQ（纳斯达克 100 ETF） (`qqq`) | 739.61 | 美元 | 0 … 930 | 估算 |
|  | 黄金价格 (`gold`) | 4319.0 | 美元/盎司 | 0 … 6300 |  |
|  | TLT（20 年以上美国国债 ETF） (`tlt`) | 76.01 | 美元 | 30 … 200 | 估算 |
|  | VIX 波动率指数 (`vix`) | 16.3 | 指数 | 0 … 110 |  |
|  | Case-Shiller 全国房价指数 (`case_shiller`) | 334.2 | 指数（2000-01=100） | 0 … 410 |  |
|  | FHFA 房价指数（仅购买） (`fhfa_hpi`) | 444.6 | 指数（1991-01=100） | 10 … 540 |  |
| 居民 | 个人储蓄率 (`saving_rate`) | 3.8 | % 可支配收入 | 0 … 40 |  |
|  | 家庭偿债负担 (`hh_debt_service`) | 11.11 | % 可支配收入 | 7 … 18 |  |
|  | 消费者信心（密歇根） (`consumer_sentiment`) | 48.1 | 指数（1966Q1=100） | 20 … 130 |  |
| 外部、能源与贸易 | 贸易加权美元指数（广义） (`usd_index`) | 121.0 | 指数 | 60 … 200 |  |
|  | WTI 原油价格 (`wti`) | 97.2 | 美元/桶 | 0 … 180 |  |
|  | 汽油零售价 (`gasoline`) | 4.27 | 美元/加仑 | 1 … 8 |  |
|  | 战略石油储备库存 (`spr`) | 282.6 | 百万桶 | 0 … 750 |  |
|  | 有效关税率 (`tariff_rate`) | 6.85 | % | 0 … 14 |  |
|  | 人民币兑美元 (`cny_usd`) | 6.67 | 元/美元 | 5.5 … 8.5 |  |
| 信心与不确定性 | 经济政策不确定性指数 (`epu`) | 207.6 | 指数 | 0 … 910 |  |
|  | 地缘政治风险指数 (`gpr`) | 136.8 | 指数 | 0 … 380 |  |
| 政治与制度（定性） | 美联储政策立场 (`fed_stance`) | 偏鹰观望（暂停加息） | enum | — | 判断 |
|  | 最近一次议息会议（10 月 27–28 日） (`fomc_last`) | 维持 | enum | — | 未确认 |
|  | 美联储主席与理事会人事 (`fed_chair`) | 按数据日期的现任；主席倾向中性 | text | — | 未确认 |
|  | IEEPA 关税的法律状态 (`ieepa_status`) | 有效 | enum | — | 未确认 |
|  | 石油供给中断（2026 年冲突） (`oil_disruption`) | 中断持续 | enum | — | 未确认 |
|  | 两院与白宫的多数党 (`political_control`) | 白宫：共和党；参议院：共和党 53–47；众议院：共和党微弱多数 | text | — | 未确认 |
|  | 众议院控制权（2026-11-03 中期选举） (`house_control`) | 选举前：共和党微弱多数（结果未揭晓） | enum | — | 未确认 |
|  | 中美贸易休战 (`china_truce`) | 休战有效（约 2026-11-10 到期） | enum | — | 未确认 |
|  | 欧佩克+ 产量政策 (`opec_policy`) | 维持配额 | enum | — | 未确认 |
|  | 债务上限状态 (`debt_limit_status`) | 接近上限 | enum | — | 判断 |
|  | 对华芯片出口管制与稀土管制 (`chip_controls`) | 按数据日期的现状 | text | — | 未确认 |

## API (JSON, under `/api`)

| Method and path | What it does |
|---|---|
| `GET /health`, `GET /settings`, `PUT /settings`, `POST /settings/test_key` | status; settings (key masked); update (`api_key`, `clear_key`, `backend`, `default_model`, `temperature`, `reasoning_effort`, `max_parallel`, `timeout_s`, `max_tokens`, `record_thinking`); check the key |
| `GET /models?refresh=1` | the model list |
| `GET /worlds`, `POST /worlds` (`{mode: blank\|template, name}`), `POST /worlds/import` | list, create, import (a first-format file is migrated) |
| `GET\|PUT\|DELETE /worlds/{id}` | read (with steps, a pending failure and `calls_per_iteration`), update settings and the `description`, delete (`{confirm: true}`) |
| `PUT /worlds/{id}/schema` (`{fields, questions?, initial_values?, initial_statuses?}`) | edit the indicators and state questions (validated; 400 with `problems`; 409 when the initial state is read-only) |
| `PUT /worlds/{id}/initial` (`{period?, values?, statuses?, narrative?}`), `/actors` (`{actors}`), `/synthesizer` (`{instructions, model, max_items_per_call}`) | edit (validated; 400 with `problems`) |
| `POST /worlds/{id}/duplicate`, `GET /worlds/{id}/export` | copy without history; download with history |
| `POST /worlds/{id}/run`, `GET /worlds/{id}/run`, `POST /worlds/{id}/retry`, `POST /worlds/{id}/discard` | start an iteration (background), live progress (actors and synthesizer steps), retry or drop a failed synthesizer attempt |
| `GET /worlds/{id}/steps`, `/steps/{n}`, `/iterations/{n}/calls`, `/pending/calls` | history (with `partial` and change counts), one step with its iteration (calls without their texts), every call in full |
| `POST /worlds/{id}/resume`, `POST /worlds/{id}/interrupted/discard`, `GET /worlds/{id}/interrupted/audit[?who=…]` | resume an interrupted or paused iteration (409 if the world changed), drop it, audit what it did (`GET /worlds/{id}` carries its summary as `interrupted`) |
| `GET /worlds/{id}/iterations/{n}/audit[?who=…]`, `/pending/audit[?who=…]` | the audit view: the participants, or one participant's final output and every call (input, thinking, output, meta); `who` is an actor id or `synthesizer:<step key>` |
| `POST /worlds/{id}/rewind` (`{step, confirm: true}`), `POST /worlds/{id}/fork` (`{step, name}`) | rewind, fork |
| `GET /worlds/{id}/estimate` | the cost estimate of the next iteration (one row per call; `counts` of actor and synthesizer calls) |

## Tests

`tests/test_worldsim_*.py` (with the fake OpenRouter and the small and first-format test worlds in
`tests/worldsim_fakes.py`) run on the mock backend or a fake HTTP transport; `tests/conftest.py` points WorldSim at a
temporary data directory and replaces its transport with one that refuses every request, so no test can reach
openrouter.ai. They cover world CRUD; schema, state-question and state validation; decision questions and autonomy
validation; the synthesizer's chunking (every field and question exactly once, at most x per call, group order,
splitting and merging) and the x setting; the narrative limit; the migration of first-format worlds (in memory, on
disk with its backup, through the API and in prompts); the actor and synthesizer prompts (description, questions,
guidance, autonomy, statuses, answers and actions, the digest, what earlier steps wrote, own items only, public
history without rationales); an iteration end to end (actors in parallel, every synthesizer step, per-step schemas
with only their own keys, validation, persistence); the failure paths (actor retry and no response, bad-JSON repair,
an unanswered required question repaired then marked, actions over the maximum or with autonomy off, a synthesizer
step carried forward and the iteration marked partial, dropped extra keys, a narrative repaired then cut, a
synthesizer that fails outright and its retry, a non-retryable error); the OpenRouter request shape; the key (0600,
masked, absent from every other saved file, response and log); the origin guard; rewind and fork; export / import;
the default world (description, 22 questions with statuses, narrative length, asset prices and their sources, the
fourteen actors' questions and autonomy, the instructions) loading without `data/`; and the UI's static checks.

## Limits

- One iteration at a time per world; progress lives in the server's memory (a restart during a run loses the run,
  never a saved step). Runs of different worlds can overlap.
- The synthesizer's steps run one after another, so an iteration takes about (1 + chunks + 1) model calls of time
  after the actors; with x = 6 the default world makes 22 synthesizer calls. Each step's prompt repeats the shared
  context (previous state, decisions), which is where most of the cost goes.
- A partial iteration is saved as it is; to redo it, rewind to the step before and run again.
- The cost estimate is rough (token counts estimated from text length; answer lengths and reasoning assumed).
- The mock backend only exercises the mechanics: its answers are short fixed phrases picked by hash and its value
  and status changes are small deterministic moves, not economics.
- Prompts are fixed in English around the world's own text; output language is the world's setting.
- Numbers in the default world are a snapshot of Causeway's local data on 2026-11-02; most pinned facts are
  unconfirmed and tagged so; QQQ and TLT are Causeway's estimates, not traded prices.
