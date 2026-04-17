# Claude Code + Memory System — Полная архитектура

## Обзор

Кастомная система, построенная поверх Claude Code (CLI), которая добавляет:
- **Персистентную память** (MCP сервер с SQLite + ChromaDB + Ollama embeddings)
- **12 хуков** для автоматизации на каждом этапе работы
- **Self-improvement pipeline** (автоматическое обучение на ошибках)
- **Трёхстрочный statusline** с мониторингом контекста, лимитов и затрат
- **Локальную AI-модель** (Ollama vitalii-brain) для offline-справок
- **Recovery-систему** для восстановления контекста после сбоев

---

## Как это работает: полный цикл сессии

### 1. Старт сессии

```
Пользователь открывает Claude Code в IDE (VS Code / терминал)
         │
         ▼
   ┌─────────────────────────────────────┐
   │  SessionStart hook (session-start.sh)│
   │                                      │
   │  1. Открывает Obsidian (если закрыт)│
   │  2. macOS уведомление: "New session" │
   │  3. Проверяет recovery файлы         │
   │     (если прошлая сессия оборвалась) │
   │  4. Считает авто-сохранённые         │
   │     транскрипты из extract-queue/    │
   │  5. Загружает SOUL rules из memory.db│
   │     (показывает активные правила)    │
   │  6. Показывает знания о проекте:     │
   │     "Project: 141 records, 48 sol" │
   │  7. Чистит старые error-fix states   │
   │  8. Сбрасывает context threshold     │
   │     флаги (70/85/95%)                │
   └─────────────────────────────────────┘
         │
         ▼
   Claude читает CLAUDE.md:
   - Memory-First Rule → обязательный memory_recall перед работой
   - Git Safety → запрет на git add/commit/push
   - Docker Rule → всё через контейнеры
   - Tech Stack → PHP 8.4, Go 1.25+, Vue 3.6, PostgreSQL 18
```

### 2. Начало работы над задачей

```
Пользователь: "Добавь REST API для заказов"
         │
         ▼
   ┌─────────────────────────────────────┐
   │  Claude (с CLAUDE.md инструкциями)   │
   │                                      │
   │  1. memory_recall("orders API REST", │
   │     project="ImPatient")             │
   │     → Ищет готовые решения           │
   │     → 4-tier search:                 │
   │       FTS5 → ChromaDB → Fuzzy → Graph│
   │                                      │
   │  2. self_rules_context(project=...)  │
   │     → Загружает правила поведения    │
   │     → "Always include ALTER TABLE    │
   │        migration for existing tables"│
   │                                      │
   │  3. Если сложная задача → /interview │
   │     → Задаёт вопросы до понимания    │
   │                                      │
   │  4. Если найден reusable рецепт →    │
   │     использует как базу, не с нуля   │
   └─────────────────────────────────────┘
```

### 3. Написание кода

```
Claude пишет/редактирует файл
         │
         ▼
   ┌──────────────────────────────────────┐
   │  PostToolUse:Write|Edit              │
   │  → post-write.sh                     │
   │                                      │
   │  Автоформатирование по расширению:   │
   │  .php → php-cs-fixer                 │
   │  .go  → gofmt + goimports           │
   │  .ts/.vue/.css → prettier            │
   │  .json → jq                          │
   │  .py  → black / ruff                 │
   │  .sql → pg_format                    │
   │  .rs  → rustfmt                      │
   └──────────────────────────────────────┘
```

### 4. Выполнение bash-команд

```
Claude хочет выполнить bash-команду
         │
         ▼
   ┌──────────────────────────────────────┐
   │  PreToolUse:Bash                     │
   │  → validate-command.sh               │
   │                                      │
   │  Проверяет 36 запрещённых паттернов: │
   │  ❌ git push/add/commit              │
   │  ❌ docker push, ssh, scp            │
   │  ❌ kubectl apply/delete             │
   │  ❌ rm -rf /, sudo                   │
   │  ❌ Co-Authored-By в коммитах        │
   │  ❌ pipe to bash/sh                  │
   │                                      │
   │  Если паттерн найден → exit 2        │
   │  (команда ЗАБЛОКИРОВАНА)             │
   └──────────────────────────────────────┘
         │ (если разрешена)
         ▼
   ┌──────────────────────────────────────┐
   │  PreToolUse:* (любой инструмент)     │
   │  → context-auto-save.sh              │
   │                                      │
   │  Проверяет threshold флаги:          │
   │  70% → "Сохрани важные данные"       │
   │  85% → Пошаговая инструкция сохран.  │
   │  95% → ОБЯЗАТЕЛЬНЫЙ 6-шаговый       │
   │        протокол сохранения           │
   │        (без спроса у пользователя!)  │
   └──────────────────────────────────────┘
         │
         ▼
   Команда выполняется
         │
         ▼
   ┌──────────────────────────────────────┐
   │  PostToolUse:Bash                    │
   │  → memory-trigger.sh (5 частей)      │
   │                                      │
   │  ЧАСТЬ 1: Ошибка? (exit_code != 0)  │
   │  → Классификация: timeout, api_error,│
   │    config_error, wrong_assumption,    │
   │    code_error, OOM                    │
   │  → Инструкция: self_error_log(...)   │
   │  → Сохраняет error state для трекинга│
   │  → Считает повторы (3+ → insight)    │
   │                                      │
   │  ЧАСТЬ 2: Error→Fix Pair Detection   │
   │  → Если предыдущая ошибка <10 мин    │
   │    и текущая похожая команда прошла → │
   │    "Ошибка исправлена! Сохрани фикс" │
   │  → Предлагает memory_save(solution)  │
   │                                      │
   │  ЧАСТЬ 3: Rate SOUL Rules            │
   │  → Тесты прошли? → rate rules        │
   │  → Билд прошёл? → rate rules         │
   │                                      │
   │  ЧАСТЬ 4: Значимые команды           │
   │  → git commit → "Сохрани в память"   │
   │  → docker compose up → напоминание   │
   │  → migration → "Сохрани схему"       │
   │                                      │
   │  ЧАСТЬ 5: Завершение задачи          │
   │  → Полный тест-сьют прошёл →         │
   │    "Сохрани как reusable solution"    │
   │  → Линтер прошёл →                   │
   │    "Сохрани convention если есть"     │
   └──────────────────────────────────────┘
         │
         ▼
   ┌──────────────────────────────────────┐
   │  PostToolUse:Bash                    │
   │  → log-command.sh                    │
   │                                      │
   │  Записывает в command-history.log:   │
   │  [timestamp] [cwd] command           │
   │  (ротация: последние 10000 строк)   │
   └──────────────────────────────────────┘
```

### 5. Делегирование субагентам

```
Claude запускает субагента (Agent tool)
         │
         ▼
   ┌──────────────────────────────────────┐
   │  Перед запуском (CLAUDE.md правило): │
   │  1. memory_recall по теме задачи     │
   │  2. Найденные рецепты → в промпт     │
   │  3. Субагент получает контекст       │
   └──────────────────────────────────────┘
         │
         ▼
   Субагент работает (до 8 параллельно)
   - Модель: Opus (agentSettings.defaultModel)
   - Таймаут: 10 мин (timeout: 600000)
   - Может искать в памяти через:
     ~/claude-memory-server/ollama/lookup_memory.sh
         │
         ▼
   ┌──────────────────────────────────────┐
   │  TaskCompleted hook                  │
   │  → task-completed.sh                 │
   │                                      │
   │  SUCCESS → "Сохрани как reusable"    │
   │  FAILED  → "Логируй ошибку"         │
   │  macOS уведомление: "Task done"      │
   └──────────────────────────────────────┘
         │
         ▼
   ┌──────────────────────────────────────┐
   │  TeammateIdle hook                   │
   │  → teammate-idle.sh                  │
   │                                      │
   │  Фоновый агент завершился →          │
   │  macOS уведомление со звуком:        │
   │  completed → Glass                   │
   │  failed → Basso                      │
   │  waiting → Tink                      │
   └──────────────────────────────────────┘
```

### 6. Переключение проекта

```
Claude переходит в другую директорию
         │
         ▼
   ┌──────────────────────────────────────┐
   │  CwdChanged hook                     │
   │  → cwd-changed.sh                    │
   │                                      │
   │  1. Определяет новый проект + branch │
   │  2. Запрашивает memory.db:           │
   │     - Кол-во knowledge records       │
   │     - Кол-во SOUL rules              │
   │     - Recent errors (7 дней)         │
   │  3. Выводит инструкции:              │
   │     "Load context: memory_recall()"  │
   │     "Load rules: self_rules_context()"│
   └──────────────────────────────────────┘
```

### 7. Мониторинг в реальном времени (Statusline)

```
Statusline обновляется на КАЖДОМ ответе Claude
         │
         ▼
   ┌──────────────────────────────────────────────────────────────────┐
   │  statusline.sh (18ms, pure bash)                                │
   │                                                                  │
   │  Строка 1: Модель | Контекст | Токены | Кэш                    │
   │  ┌──────────────────────────────────────────────────────────┐    │
   │  │ Opus | [████████░░░░░░░░░░░░] 42% of 1M #2 | ↓85K ↑12K │    │
   │  └──────────────────────────────────────────────────────────┘    │
   │                                                                  │
   │  Строка 2: User | Стоимость | Время | Строки | Git | Dir       │
   │  ┌──────────────────────────────────────────────────────────┐    │
   │  │ vitalii@Mac | $2.45 | ⏱ 12m (api 8m) | +340/-52 | ⎇ main │   │
   │  └──────────────────────────────────────────────────────────┘    │
   │                                                                  │
   │  Строка 3: Rate limits (API usage)                              │
   │  ┌──────────────────────────────────────────────────────────┐    │
   │  │ 5h [██████████] 100% ↻1h40m | 7d [██████████] 100% ↻6d │    │
   │  └──────────────────────────────────────────────────────────┘    │
   │                                                                  │
   │  Дополнительно:                                                 │
   │  - Отслеживает context compaction (#1 → #2 → #3...)            │
   │  - При 70/85/95% пишет threshold флаги в .context-flags/       │
   │  - macOS уведомления при 85% и 95%                              │
   │  - Фоновый запрос usage limits (кэш 2 мин)                     │
   └──────────────────────────────────────────────────────────────────┘
```

### 8. Уведомления

```
Любое событие Claude Code
         │
         ▼
   ┌──────────────────────────────────────┐
   │  Notification hook                   │
   │  → notify.sh                         │
   │                                      │
   │  permission_prompt → Glass           │
   │  idle_prompt → Tink                  │
   │  auth_success → Glass                │
   │  другое → default                    │
   │                                      │
   │  Формат: "project @ branch | event"  │
   └──────────────────────────────────────┘
```

### 9. Остановка сессии (Context Limit / Ctrl+C)

```
Сессия обрывается (лимит контекста, ошибка)
         │
         ▼
   ┌──────────────────────────────────────┐
   │  Stop hook (on-stop.sh)              │
   │                                      │
   │  1. Сохраняет git state:             │
   │     branch, modified files,          │
   │     последние 5 коммитов             │
   │  2. Создаёт recovery файл:           │
   │     ~/.claude/recovery/pending-*.md  │
   │  3. Извлекает знания из транскрипта  │
   │     (extract_transcript.py → фон)    │
   │  4. macOS уведомление: "Stopped"     │
   │  5. Хранит последние 5 recovery      │
   └──────────────────────────────────────┘
```

### 10. Завершение сессии (нормальное)

```
Пользователь делает /clear, /compact, или выходит
         │
         ▼
   ┌──────────────────────────────────────┐
   │  SessionEnd hook (session-end.sh)    │
   │                                      │
   │  1. Извлекает из транскрипта:        │
   │     - Последние 5 сообщений юзера    │
   │     - Последний контекст Claude      │
   │  2. Создаёт recovery файл с          │
   │     детальным контекстом             │
   │  3. AUTO-SAVE в MCP Memory:          │
   │     auto_session_save.py → фон       │
   │     (сохраняет summary как fact)     │
   │  4. Извлечение знаний:               │
   │     extract_transcript.py → фон      │
   │     (парсит транскрипт → extract-q)  │
   │  5. OLLAMA SYNC (фон):              │
   │     sync_to_ollama.sh --quick        │
   │     (обновляет system prompt модели) │
   │  6. Чистит error-fix state           │
   │  7. macOS уведомление: "Done"        │
   └──────────────────────────────────────┘
```

---

## MCP Memory Server (20 tools)

### Архитектура хранения

```
~/.claude-memory/
├── memory.db          (SQLite — 3 MB, основное хранилище)
│   ├── knowledge      (1408 записей: fact, solution, decision, lesson, convention)
│   ├── sessions       (295 сессий)
│   ├── relations      (граф связей между записями)
│   ├── observations   (легковесные наблюдения)
│   ├── errors         (лог ошибок для self-improvement)
│   ├── insights       (извлечённые паттерны)
│   └── rules          (SOUL — поведенческие правила)
│
├── chroma/            (ChromaDB — 16.5 MB, векторный индекс)
│   └── knowledge      (768-dim Ollama embeddings для semantic search)
│
├── backups/           (JSON экспорты)
└── extract-queue/     (очередь извлечения транскриптов)
```

### 4-уровневый поиск (memory_recall)

```
Запрос: "JWT authentication refresh token"
         │
         ▼
   Tier 1: FTS5 + BM25 (keyword match)
   → Ищет точные слова в content/context/tags
   → BM25 scoring для релевантности
         │
         ▼
   Tier 2: ChromaDB Semantic (vector similarity)
   → Ollama nomic-embed-text (768-dim)
   → Cosine similarity по смыслу
   → Fallback: SentenceTransformers (384-dim)
         │
         ▼
   Tier 3: Fuzzy Match (SequenceMatcher)
   → Для опечаток и частичных совпадений
   → Jaccard similarity > 0.85
         │
         ▼
   Tier 4: Graph Expansion
   → По связям (relations) между записями
   → causal, solution, context, related, contradicts
         │
         ▼
   Decay Scoring (exponential, half-life=90 дней)
   → Недавние записи ранжируются выше
   → recall_count увеличивается при каждом поиске
         │
         ▼
   LRU Query Cache (200 entries, 5 min TTL)
   → Повторные запросы мгновенно из кэша
   → Инвалидация при write/update/delete
```

### 20 MCP Tools

| # | Tool | Назначение |
|---|------|-----------|
| **Основные** | | |
| 1 | `memory_save` | Сохранить знание (decision/fact/solution/lesson/convention) |
| 2 | `memory_recall` | Поиск по памяти (4-tier search) |
| 3 | `memory_update` | Обновить запись (создаёт новую версию, supersede старую) |
| 4 | `memory_delete` | Мягкое удаление (убирает из поиска и ChromaDB) |
| 5 | `memory_search_by_tag` | Поиск по тегу (partial match) |
| 6 | `memory_history` | История версий записи (chain: newest→oldest) |
| 7 | `memory_timeline` | Навигация по сессиям (по дате, номеру, offset) |
| 8 | `memory_relate` | Создать связь между записями (graph) |
| 9 | `memory_observe` | Легковесное наблюдение (без dedup, без ChromaDB) |
| **Обслуживание** | | |
| 10 | `memory_consolidate` | Найти и объединить дубликаты (Jaccard > threshold) |
| 11 | `memory_export` | JSON бэкап в ~/.claude-memory/backups/ |
| 12 | `memory_extract_session` | Извлечение знаний из транскриптов |
| 13 | `memory_forget` | Retention policy: archive (>180d) → purge (>365d) |
| 14 | `memory_stats` | Статистика + health score |
| **Self-Improvement** | | |
| 15 | `self_error_log` | Логировать ошибку (auto-classify, pattern detection) |
| 16 | `self_insight` | Управление инсайтами (add/upvote/downvote/promote) |
| 17 | `self_patterns` | Анализ паттернов ошибок (frequency, trends, candidates) |
| 18 | `self_reflect` | Рефлексия после задачи (Reflexion pattern) |
| 19 | `self_rules` | Управление SOUL rules (list/fire/rate/suspend/activate) |
| 20 | `self_rules_context` | Загрузка правил для текущей сессии |

### Auto-Deduplication

```
Новая запись: memory_save("JWT auth with refresh tokens")
         │
         ▼
   Jaccard similarity > 0.85 с существующей?
   ИЛИ Fuzzy similarity > 0.90?
         │
    YES ──► deduplicated=true (не сохраняется)
    NO  ──► saved (+ ChromaDB embedding)
```

### Retention Zones

```
                    180 дней         365 дней
   ──── active ────┼── archived ────┼── purged ────►
                   │                │
   Условие архивации:               Условие удаления:
   - Не recalled                    - Архивирована
   - Низкий confidence              - > 365 дней
   - > 180 дней                     - Не recalled
```

---

## Self-Improvement Pipeline

```
   Ошибка (bash fail, wrong assumption, API error)
         │
         ▼
   self_error_log → errors table
   (category, severity, fix)
         │
         ▼
   3+ ошибки в одной категории за 30 дней?
         │
    YES ──► PATTERN_ALERT
         │  → self_insight(action='add', ...)
         │  → importance=2, confidence=0.5
         │
         ▼
   Insight подтверждается?
   upvote (+1 importance, +0.05 confidence)
   downvote (-1, auto-archive at 0)
         │
         ▼
   importance ≥ 5 AND confidence ≥ 0.8?
         │
    YES ──► self_insight(action='promote')
         │  → Creates SOUL Rule
         │
         ▼
   SOUL Rule (поведенческое правило)
   - Загружается на старте сессии
   - fire_count отслеживает применение
   - rate(success=true/false) оценивает
   - Auto-suspend: success_rate < 0.2 после 10+ fires

   Пример текущего правила (P7):
   "Always include ALTER TABLE migration
    for existing tables when adding new columns"
```

---

## Ollama Integration (локальная AI-модель)

```
   ┌──────────────────────────────────────┐
   │  vitalii-brain                       │
   │  (qwen2.5-coder:32b, 19GB, Q4_K_M)  │
   │                                      │
   │  System prompt: ~1725 tokens         │
   │  (top knowledge by recall*confidence)│
   └──────────────────────────────────────┘
         │
         │ 3 режима использования:
         │
   ┌─────┼─────────────────────────────────┐
   │     │                                  │
   │  1. RAG Chat (rag_chat.py)            │
   │     → Вопрос → ChromaDB search        │
   │     → Контекст → Ollama generate      │
   │     → Ответ с knowledge base          │
   │                                       │
   │  2. Lookup (lookup_memory.sh)         │
   │     → Для субагентов Claude           │
   │     → Bash-скрипт, вызывает rag_chat  │
   │     → Показывает только контекст      │
   │                                       │
   │  3. Direct (ollama run vitalii-brain) │
   │     → Быстрые вопросы в терминале     │
   │     → Без RAG, только system prompt   │
   └───────────────────────────────────────┘

   Синхронизация (автоматическая):
   SessionEnd → sync_to_ollama.sh --quick →
   export_knowledge.py → обновляет Modelfile →
   ollama create vitalii-brain
```

---

## Permissions System

### Разрешено автоматически (без вопросов)

```
READ:    Все файлы (Read, Glob, Grep)
WRITE:   src/, app/, tests/, components/, pages/, migrations/,
         *.md, *.json, *.yaml, Makefile, Dockerfile, docker-compose
EDIT:    *.php, *.go, *.ts, *.vue, *.js, *.css, *.sql, *.md, *.json
BASH:    git status/diff/log/show/branch, docker ps/logs/compose,
         make, npm/yarn/pnpm/bun, go test/build/run, php, composer,
         psql, redis-cli, curl, jq
```

### ЗАПРЕЩЕНО (exit 2, блокировка)

```
GIT:     add, commit, push, checkout, merge, rebase, tag
DEPLOY:  docker push/login, ssh, scp, rsync, kubectl, helm, terraform
DANGER:  rm -rf /, sudo, chmod 777, npm publish, pipe to bash
SECRET:  Write to **/secrets*, *.pem, *.key, .ssh/
AI:      Co-Authored-By в коммитах, упоминания Claude/AI
```

---

## Файловая структура

```
~/.claude/
├── CLAUDE.md                    # Глобальные инструкции (3000+ строк)
├── settings.json                # Конфигурация: hooks, MCP, permissions
├── statusline.sh                # Statusline скрипт (18ms)
├── rules/                       # Правила по технологиям
│   ├── go.md, php.md, vue.md
│   ├── database.md, docker.md
│   ├── git.md, bitrix.md
├── hooks/                       # 12 хуков
│   ├── lib/common.sh            # Общие функции (hook_get, hook_notify...)
│   ├── session-start.sh         # SessionStart: recovery + SOUL rules
│   ├── session-end.sh           # SessionEnd: auto-save + ollama sync
│   ├── on-stop.sh               # Stop: git state + recovery
│   ├── validate-command.sh      # PreToolUse:Bash — блокировка опасных
│   ├── context-auto-save.sh     # PreToolUse:* — auto-save при 70/85/95%
│   ├── memory-trigger.sh        # PostToolUse:Bash — error/fix/memory
│   ├── post-write.sh            # PostToolUse:Write|Edit — auto-format
│   ├── log-command.sh           # PostToolUse:Bash — audit log
│   ├── notify.sh                # Notification — macOS notifications
│   ├── cwd-changed.sh           # CwdChanged — auto-load project context
│   ├── task-completed.sh        # TaskCompleted — save reusable results
│   └── teammate-idle.sh         # TeammateIdle — background agent done
├── recovery/                    # Recovery файлы (последние 5)
├── .context-flags/              # Threshold флаги (70/85/95%)
├── .memory-state/               # Error→Fix pair tracking
├── command-history.log          # Аудит всех bash-команд
├── hooks.log                    # Лог хуков
└── .usage-cache.json            # Кэш API usage limits

~/claude-memory-server/
├── src/
│   ├── server.py                # MCP сервер (20 tools)
│   ├── cache.py                 # LRU Query Cache (200/5min)
│   ├── reembed.py               # Миграция embeddings
│   ├── auto_session_save.py     # Автосохранение сессий
│   ├── extract_transcript.py    # Извлечение знаний из транскриптов
│   ├── auto_extract_active.py   # Batch extraction
│   └── dashboard.py             # Web dashboard (port 37737)
├── ollama/
│   ├── rag_chat.py              # RAG поиск + Ollama генерация
│   ├── lookup_memory.sh         # CLI для субагентов
│   ├── export_knowledge.py      # Экспорт для обучения
│   └── sync_to_ollama.sh        # Синхронизация модели
└── .venv/                       # Python virtualenv

~/.claude-memory/
├── memory.db                    # SQLite (knowledge, sessions, rules...)
├── chroma/                      # ChromaDB (vector embeddings)
├── backups/                     # JSON экспорты
└── extract-queue/               # Очередь извлечения транскриптов

~/Documents/project/             # Obsidian vault
└── (проектная документация для человека)
```

---

## Двойная система хранения

| | MCP Memory (для агентов) | Obsidian (для человека) |
|---|---|---|
| **Формат** | SQLite + ChromaDB | Markdown файлы |
| **Поиск** | 4-tier (FTS→semantic→fuzzy→graph) | Obsidian search + MCP |
| **Цель** | Кросс-сессионный machine-readable поиск | Читаемые отчёты, документация |
| **Что хранится** | Решения, факты, конвенции, ошибки | Сессии, задачи, архитектура, troubleshooting |
| **Автоматизация** | memory_save/recall через hooks | Ручная + скиллы (/save, /compact) |

---

## Ключевые числа

| Метрика | Значение |
|---------|----------|
| Активных записей | 1408 |
| Reusable рецептов | 82 |
| Всего сессий | 295 |
| Проектов | 50+ |
| Solutions | 280 |
| SOUL Rules | 1 active |
| Health Score | 0.80 |
| Хранилище | 20 MB (SQLite 3 + ChromaDB 16.5) |
| Хуков | 12 |
| MCP Tools | 20 |
| Statusline | 18ms (3 строки) |
| Embedding | Ollama nomic-embed-text (768-dim) |
| Локальная модель | vitalii-brain (qwen2.5-coder:32b, 19GB) |
| LSP плагины | 3 (gopls, php, typescript) |
