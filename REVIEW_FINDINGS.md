# Code Review: Gem Search

## Remediation status (2026-10-10)

Every claim was re-verified against the code before any change was made. Evidence: `tests/test_security.py`, `tests/test_launch_safety.py`, `tests/http_smoke.py` (run against the Docker container), and the Docker verification described in `docs/DOCKER.md`.

| # | Finding | Status | Evidence / notes |
|---|---|---|---|
| P0 | `GET /api/state` returned the dashboard token and extension pairing key to any local caller. That token authorized every POST, including `/api/launch-control` | **Fixed** | Reproduced on the original code: an unauthenticated read harvested the token, which then paused the queue (HTTP 200). The fix is a persisted sign-in token (`data/dashboard-token`, 0600, never served) and an HttpOnly SameSite=Strict session cookie, plus a derived CSRF header and an Origin/Sec-Fetch-Site check, or Bearer auth for local scripts. Unauthenticated `/api/state` returns 401. The pairing key is shown only to a signed-in dashboard. `DashboardAuthTests` covers every privileged route |
| P1 | Live mode could be selected by environment configuration | **Fixed (hardened)** | The original check was already an exact `== 'live'` match. Added `GEM_SEARCH_DRY_RUN_LOCK=1` (set in the Dockerfile and Compose), which overrides `LAUNCH_MODE`. Mode, provider configuration and job shape are re-checked at the execution boundary before any provider call. Jobs stay pinned to their enqueue mode. Covered by `LaunchModeTests` |
| P1 | Feed/research content could authorize a launch | **Fixed (verified separation)** | Drafts are built only from fixed fields. Project-supplied `mode`, `provider` and `amount` are ignored. Feed ingestion with hostile input is tested in dry-run with no provider calls (`FeedTests`) |
| 1 | `enqueue()` topic dedup race | **Fixed** | Topic check and insert now run in one `BEGIN IMMEDIATE` transaction. An 8-thread concurrent test produces exactly one job. The suggested `fcntl` lock was unnecessary because SQLite serializes the write |
| 2 | No draft validation before the subprocess | **Fixed** | `valid_draft()` runs at the execution boundary (id, mode, amount=0, name, symbol, provider). A tampered job becomes `blocked` with no provider call. Also fixed the job result update, which was keyed on the payload id instead of the row id |
| 3 | Unprocessed spider posts block ingestion | **Fixed (differently)** | Stale (>24 h) unprocessed posts are retired to `processed=1` instead of being deleted, so research rows are preserved and pruned by the existing 7-day retention |
| 4 | Regex DoS in `jev.phrases()` | **Not reproducible** | The pattern has no nested quantifiers (linear time) and input is capped upstream. No change |
| 5 | Grok response indexing | **Not a defect** | The `KeyError`/`IndexError` is caught and converted to an unavailable `hold` vote. No change |
| 6 | Grok cache key versioning | **Deferred** | The model name is already part of the key and entries expire after 24 h. Low value |
| Docker | Read-only root filesystem | **Fixed** | `read_only: true`, a 16 MB `noexec` tmpfs at `/tmp`, root-owned app code, digest-pinned base image, 1 CPU, capped logs, and `/healthz` as the health check |

Additional fixes found during remediation: atomic, self-repairing secret files (a truncated `extension-key` previously disabled pairing permanently). Invalid inbox files move to `failed/` instead of retrying forever, and storage failures keep the file for a retry. Retention covers `processed/` and `failed/` (30 days) and expired crawl cache rows. Storage errors return 500 without paths. A failure to write the pause marker is reported, and the queue stays paused in memory.

---

## Executive Summary
Comprehensive review of 5 Python modules + Docker/Compose config. **1 high-severity issue** (race condition), **3 medium-severity** (unbounded queues, validation gaps), **2 low-severity** (edge case error handling). No critical vulnerabilities; security hardening (SQL parameterization, validation, sandboxing) is well-implemented.

---

## High-Severity Issues

### 1. Race Condition: `enqueue()` Writes Without Lock

**File**: `automation.py`
**Lines**: ~145 (enqueue), ~175 (tick)
**Severity**: HIGH

**Issue**:
- `enqueue()` inserts into `launch_jobs` **without** acquiring the launcher lock (`fcntl.flock`)
- `_tick()` holds the lock while reading/updating jobs, but `enqueue()` runs concurrently (called from main event loop)
- Result: A job can be inserted between the deduplication check and commit, bypassing the `topic` uniqueness constraint

**Proof**:
```python
# In _tick():
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
try:
    con.execute('BEGIN IMMEDIATE')
    # Job read/write here
finally:
    fcntl.flock(lock, fcntl.LOCK_UN)

# In enqueue() — NO LOCK:
with self.connect() as con:
    result = con.execute('INSERT OR IGNORE INTO launch_jobs ...')
```

**Impact**:
- Two jobs with the same `topic` could be queued in dry-run mode (should only allow one per 7 days)
- Potential for duplicate launches if `observed_at` window is wide

**Fix**:
Move topic deduplication check into `_tick()` under the lock:

```python
def _tick(self):
    mode = self.mode()
    with self.connect() as con:
        con.execute('BEGIN IMMEDIATE')
        # NEW: Check for pending topic-scoped jobs FIRST (under lock)
        pending_jobs = con.execute(
            "SELECT id FROM launch_jobs WHERE mode=? AND topic IS NOT NULL AND status IN ('queued','running','pending')",
            (mode,)
        ).fetchall()
        pending_topics = set()
        for row in pending_jobs:
            job_data = con.execute("SELECT payload FROM launch_jobs WHERE id=?", (row[0],)).fetchone()
            if job_data:
                pending_topics.add(json.loads(job_data[0]).get('topic'))
```

Or: Acquire the lock in `enqueue()` before writing:
```python
def enqueue(self, project):
    # ... validation ...
    with open(self.data / 'launcher.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            # ... dedup check & insert ...
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
```

---

## Medium-Severity Issues

### 2. Unbounded Subprocess Input in `_tick()`

**File**: `automation.py`
**Lines**: ~160
**Severity**: MEDIUM

**Issue**:
```python
proc = subprocess.run(['node', str(self.root / 'launch/worker.mjs'), 'run'],
                      input=json.dumps(draft),  # Entire draft dict as JSON
                      text=True, capture_output=True, timeout=150, cwd=self.root)
```

The `draft` dict is built from user-controlled `project` data (name, description, URL, narrative). While JSON is safe, the Node worker may not validate all fields before use.

**Impact**: Medium
- If Node worker has injection vulnerabilities, it receives user input
- `draft['image_base64']` is generated locally but could be large (affects memory)
- No schema validation before subprocess call

**Fix**:
```python
def _tick(self):
    # ... existing code ...
    draft = json.loads(row['payload'])

    # VALIDATE draft structure before subprocess
    required_fields = {'id', 'name', 'symbol', 'description', 'narrative', 'source_url', 'status'}
    if not required_fields.issubset(draft.keys()):
        result = {'status': 'blocked', 'reason': 'Invalid draft structure'}
    else:
        # Proceed with subprocess
        try:
            proc = subprocess.run(...)
            result = json.loads(proc.stdout.strip().splitlines()[-1])
        except (subprocess.TimeoutExpired, ValueError, IndexError, OSError):
            result = {'status': 'pending', 'reason': 'Worker interrupted...'}
```

---

### 3. Unprocessed Posts Block New Ingestion in `jev.py`

**File**: `jev.py`
**Lines**: ~35 (ingest method)
**Severity**: MEDIUM

**Issue**:
```python
def ingest(self, data):
    posts = normalize_capture(data)
    added = 0
    with self.connect() as con:
        pending = con.execute('SELECT COUNT(*) FROM spider_posts WHERE processed=0').fetchone()[0]
        if pending + len(posts) > 500:
            raise ValueError('Local queue full; let crawler catch up')
```

- Hard limit of 500 unprocessed posts
- If a batch of posts is never marked `processed=1`, they block **all** future ingestion
- No age-based pruning while `processed=0`; stale posts trapped in queue

**Impact**:
- DoS: A crawler that ingests but never calls `ack()` can block new data indefinitely
- User sees "queue full" error, but data is stuck

**Fix**:
```python
def ingest(self, data):
    posts = normalize_capture(data)
    added = 0
    with self.connect() as con:
        # Clean up OLD unprocessed posts (e.g., older than 24h)
        con.execute(
            'DELETE FROM spider_posts WHERE processed=0 AND created<?',
            (time.time() - 86400,)  # 24 hours
        )
        pending = con.execute('SELECT COUNT(*) FROM spider_posts WHERE processed=0').fetchone()[0]
        if pending + len(posts) > 500:
            raise ValueError('Local queue full; let crawler catch up')
        # ... rest ...
```

---

### 4. Regex DoS Risk in `jev.py` — `phrases()` Function

**File**: `jev.py`
**Lines**: ~190–210
**Severity**: MEDIUM (low practical impact)

**Issue**:
```python
def phrases(text):
    text = re.sub(r'https?://\S+|[@$#]\w+', '', text.lower())
    words = WORD.findall(text)  # WORD = r'[a-zа-яё][a-zа-яё0-9]{2,}'
    # ... zip pairs ...
```

- Input text is up to 10,000 characters (from `text` in posts)
- Regex `WORD` is simple but run on every post; no backtracking but unbounded iterations
- If text is pathological (e.g., all similar-length words), could be slow

**Impact**: Low
- Pattern is non-backtracking (safe from catastrophic backtracking)
- Limit of 500 posts and 10k chars each = bounded worst case
- But worth optimizing

**Fix**:
```python
# Pre-compile and add explicit bounds
WORD = re.compile(r'[a-zа-яё][a-zа-яё0-9]{2,20}')  # Max 20 chars per word

def phrases(text):
    text = re.sub(r'https?://\S+|[@$#]\w+', '', text[:10000].lower())  # Cap input
    words = WORD.findall(text)[:500]  # Cap output
    # ... rest ...
```

---

## Low-Severity Issues

### 5. Missing Bounds Check Before API Response Indexing in `grok.py`

**File**: `grok.py`
**Lines**: ~95–98
**Severity**: LOW

**Issue**:
```python
data = json.loads(body)
value = json.loads(data['choices'][0]['message']['content'])
```

No validation that `data['choices']` exists or has at least one element. If API returns `{"choices": []}` or missing key, will `KeyError`.

**Impact**: Low
- Currently caught by outer `except Exception` block
- But error message is generic; could hide issues

**Fix**:
```python
try:
    with urlopen(request, timeout=45) as response:
        body = response.read(100_001)
        if len(body) > 100_000:
            raise ValueError('Oversized response')
        data = json.loads(body)

    # Explicit validation
    if not isinstance(data.get('choices'), list) or not data['choices']:
        raise ValueError('API returned empty choices')

    message_content = data['choices'][0].get('message', {}).get('content')
    if not message_content:
        raise ValueError('API returned missing content')

    value = json.loads(message_content)
    result = validate_vote(value, {e['id'] for e in evidence})
    # ... rest ...
```

---

### 6. Cache Key Collision Risk in `grok.py`

**File**: `grok.py`
**Lines**: ~60–62
**Severity**: LOW

**Issue**:
```python
identity = hashlib.sha256((config['model'] + ':v1:' + encoded).encode()).hexdigest()
with self.connect() as con:
    cached = con.execute('SELECT payload FROM grok_cache WHERE id=? AND created>?',
                         (identity, time.time()-86400)).fetchone()
```

Cache key includes model name (e.g., `grok-4.7`) and hardcoded version string `:v1:`. If:
- Model is updated to `grok-4.8` but someone changes `GROK_MODEL` env var
- Old cache with version `:v1:` is orphaned but stays in DB (eventually pruned after 24h)

**Impact**: Low
- 24-hour TTL means stale cache is auto-cleared
- But if model behavior changes mid-day, users get outdated reviews

**Fix**:
```python
# Include model version more explicitly
model_version = os.getenv('GROK_MODEL_VERSION', '1')
identity = hashlib.sha256(
    (config['model'] + ':' + model_version + ':' + encoded).encode()
).hexdigest()
```

---

## Well-Implemented Security Practices ✓

1. **SQL Parameterization**: All queries use `?` placeholders; no raw string formatting
2. **Input Validation**: Extensive checks in `normalize_capture()`, `validate_posts()`, `validate_vote()`
3. **Redirect Blocking**: Explicit validation in `fetch()` (tested in `test_block_private_redirect`)
4. **File Permissions**: Extension key created with `0o600` (owner-only read/write)
5. **Docker Sandboxing**:
   - Non-root user (UID 10001)
   - `CAP_DROP: ALL` + `no-new-privileges`
   - Memory limit (512MB) + PID limit (128)
   - `init: true` for signal handling

---

## Testing Coverage

Tests exist for most critical paths:
- ✓ Demo consensus & rejection logic
- ✓ Deduplication persistence
- ✓ Local fetch blocking
- ✓ Private redirect blocking
- ✓ Budget & rate limiting
- ✓ Icon generation

**Missing tests**:
- Race condition between `enqueue()` and `_tick()` (timing-dependent; hard to test)
- Subprocess output validation edge cases
- Grok API error handling (currently assumes stable structure)

---

## Recommendations (Priority Order)

1. **[HIGH]** Fix race condition: Move topic dedup into `_tick()` or lock `enqueue()`
2. **[MEDIUM]** Add age-based pruning to `jev.ingest()` to prevent queue starvation
3. **[MEDIUM]** Add explicit `draft` schema validation before subprocess in `_tick()`
4. **[LOW]** Add bounds check in `grok._seat()` before indexing API response
5. **[LOW]** Cap regex iterations in `jev.phrases()` by pre-compiling with length limit
6. **[LOW]** Document cache key format and versioning strategy

---

## Docker & Deployment Notes

- Dockerfile is well-hardened (already security-focused)
- Healthcheck properly uses HTTP request (not just process alive)
- `init: true` correctly handles signal propagation
- Consider: Add `read-only` filesystem and mount `/app/data` RW explicitly for better isolation
