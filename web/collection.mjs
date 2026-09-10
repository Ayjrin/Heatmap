/* Public collection control: retries reuse a UUID until the server acknowledges it. */
export const ACTIVE = new Set(['starting', 'running']);
export const MODES = { smoke: 50, small: 250, full: null };

export class CollectionController {
  constructor({ fetcher = globalThis.fetch.bind(globalThis), uuid = () => crypto.randomUUID(), changed = () => {} } = {}) {
    this.fetcher = fetcher; this.uuid = uuid; this.changed = changed;
    this.value = { run: null, dataset: null }; this.pending = null;
    this.sending = false; this.message = ''; this.polling = null;
  }
  get busy() { return this.sending || ACTIVE.has(this.value.run?.status); }

  async api(path, options = {}) {
    const abort = new AbortController(), timer = setTimeout(() => abort.abort(), 12000);
    try {
      const result = await this.fetcher(path, { cache: 'no-store', ...options, signal: abort.signal });
      const body = await result.json();
      if (!result.ok) {
        const error = new Error(typeof body.error === 'string' ? body.error : 'The collection service is unavailable.');
        error.retryable = body.retryable !== false && result.status !== 400;
        throw error;
      }
      if (!body || !Object.hasOwn(body, 'run')) throw new Error('The collection service returned an invalid response.');
      return body;
    } catch (error) {
      if (error.name === 'AbortError') throw new Error('The request timed out. Retry to check the same collection.');
      throw error;
    } finally { clearTimeout(timer); }
  }

  accept(value) {
    this.value = value; this.message = '';
    if (this.pending && value.run?.run_id === this.pending.requestId) this.pending = null;
    this.changed(this);
  }

  poll() {
    if (this.polling) return this.polling;
    this.polling = this.api('/api/status').then(value => this.accept(value)).catch(() => {
      this.message = 'Collection status is unavailable. It will retry automatically.';
      this.changed(this);
    }).finally(() => { this.polling = null; });
    return this.polling;
  }

  async start(mode) {
    if (!Object.hasOwn(MODES, mode) || this.busy) return;
    if (this.pending && this.pending.mode !== mode) return;
    if (!this.pending) this.pending = { mode, requestId: this.uuid() };
    this.sending = true; this.message = 'Checking the Riot key and starting collection…'; this.changed(this);
    try {
      const value = await this.api('/api/runs', { method: 'POST',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(this.pending) });
      this.pending = null; this.accept(value);
    } catch (error) {
      if (error.retryable === false) this.pending = null;
      this.message = this.pending
        ? `${error.message} Click the same collection button to retry safely.` : error.message;
    } finally { this.sending = false; this.changed(this); }
  }
}

export function runDescription(run) {
  if (!run) return 'Ready to collect ranked games.';
  // A rebuild republishes games collected earlier, so 'Collecting' misreads it
  // and every count it would report reads zero.
  if (run.mode === 'rebuild') {
    const rebuilds = { starting: 'Preparing to rebuild the published dataset',
      running: 'Rebuilding the published dataset from every collected game',
      succeeded: 'Published dataset rebuilt', failed: 'Dataset rebuild failed',
      paused: 'Dataset rebuild stopped' };
    return rebuilds[run.status] || 'Dataset rebuild status';
  }
  const labels = { starting: 'Starting', running: 'Collecting', auth_required: 'Riot key needs updating',
    failed: 'Collection failed', succeeded: 'Collection complete', paused: 'Collection paused' };
  // The stage names the work in flight — an update reads every ladder history
  // before it fetches its first game — so it is what keeps a long run legible
  // now that the progress bar carries the counts on its own.
  const stage = typeof run.stage === 'string' ? run.stage.replaceAll('_', ' ') : '';
  return `${labels[run.status] || 'Collection status'}${stage ? ` · ${stage}` : ''}`;
}
