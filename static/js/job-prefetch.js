/**
 * Phonos Job Prefetch & Cache
 * ---------------------------------------------------------------------------
 * Makes the app feel fast on slow / flaky connections by caching job artifact
 * files in the Cache Storage API and prefetching them ahead of time:
 *
 *   1. When a job output is open (view_output), ALL of that job's files are
 *      cached in the background so switching between structures/poses/ligands
 *      is instant — even offline.
 *   2. The most-recently-created jobs (last 5 by default) have their output
 *      files prefetched so opening them later is instant.
 *
 * Design notes:
 *   - No Service Worker required. We use `window.caches` directly (available in
 *     any secure context: https or localhost) plus a cache-aware fetch wrapper
 *     (PhonosJobCache.fetch) that the page routes artifact downloads through.
 *   - Job artifacts of completed jobs are effectively immutable, so we use a
 *     cache-first strategy — the single most useful behaviour on a slow link.
 *   - Everything degrades silently to a plain network fetch when Cache Storage
 *     is unavailable or quota is exhausted. It never blocks or breaks the page.
 *   - An LRU index (in localStorage) bounds how many jobs we keep cached.
 */
(function () {
    'use strict';

    var CACHE_NAME = 'phonos-job-files-v1';
    var INDEX_KEY = 'phonos_job_cache_index';   // LRU list of cached job_ids (most-recent first)
    var MAX_JOBS = 10;                           // keep at most this many jobs' files cached
    var RECENT_LIMIT = 5;                         // how many recent jobs to prefetch
    var MAX_CONCURRENCY = 3;                      // parallel artifact downloads
    var MAX_ARTIFACT_BYTES = 30 * 1024 * 1024;   // skip caching very large single files (PAE dumps etc.)

    // ---------------------------------------------------------------------
    // Capability / connection helpers
    // ---------------------------------------------------------------------
    function supported() {
        try {
            return !!(window.caches && window.fetch && window.localStorage);
        } catch (e) {
            return false;
        }
    }

    // Respect the user's explicit "save data" preference: don't burn their
    // metered bandwidth on speculative prefetching. We still prefetch on plain
    // slow connections (2g/3g) — that's exactly when the cache pays off — but a
    // user who opted into Data Saver has asked us not to.
    function prefetchAllowed() {
        try {
            var c = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
            if (c && c.saveData) return false;
        } catch (e) { /* ignore */ }
        return true;
    }

    function isCompletedStatus(status) {
        var s = String(status || '').toLowerCase();
        return s === 'completed' || s === 'complete' || s === 'success' ||
               s === 'succeeded' || s === 'finished' || s === 'done';
    }

    // ---------------------------------------------------------------------
    // URL builders
    // ---------------------------------------------------------------------
    function artifactUrl(jobId, filename) {
        return '/download_job_artifact/' + encodeURIComponent(jobId) + '/' + encodeURIComponent(filename);
    }

    function jobPathPrefixes(jobId) {
        return [
            '/download_job_artifact/' + encodeURIComponent(jobId) + '/',
            '/download_output/' + encodeURIComponent(jobId)
        ];
    }

    // ---------------------------------------------------------------------
    // LRU index (localStorage) — tracks which jobs are cached, most-recent first
    // ---------------------------------------------------------------------
    function readIndex() {
        try {
            var raw = localStorage.getItem(INDEX_KEY);
            var arr = raw ? JSON.parse(raw) : [];
            return Array.isArray(arr) ? arr : [];
        } catch (e) {
            return [];
        }
    }

    function writeIndex(list) {
        try {
            localStorage.setItem(INDEX_KEY, JSON.stringify(list));
        } catch (e) { /* ignore quota / private mode */ }
    }

    // Move jobId to the front; return job_ids that fell off the end (to purge).
    function touchJob(jobId) {
        var list = readIndex().filter(function (id) { return id !== jobId; });
        list.unshift(jobId);
        var keep = list.slice(0, MAX_JOBS);
        var evicted = list.slice(MAX_JOBS);
        writeIndex(keep);
        return evicted;
    }

    // ---------------------------------------------------------------------
    // Small concurrency-limited queue runner
    // ---------------------------------------------------------------------
    function runQueue(items, worker, concurrency) {
        return new Promise(function (resolve) {
            var i = 0, active = 0, done = 0;
            var n = items.length;
            if (!n) return resolve();
            function pump() {
                while (active < concurrency && i < n) {
                    var item = items[i++];
                    active++;
                    Promise.resolve()
                        .then(function () { return worker(item); })
                        .catch(function () { /* one bad file shouldn't stop the rest */ })
                        .then(function () {
                            active--; done++;
                            if (done >= n) resolve();
                            else pump();
                        });
                }
            }
            pump();
        });
    }

    function onIdle(fn, timeout) {
        if (typeof window.requestIdleCallback === 'function') {
            window.requestIdleCallback(fn, { timeout: timeout || 4000 });
        } else {
            setTimeout(fn, 1200);
        }
    }

    // ---------------------------------------------------------------------
    // Core: cache-aware fetch (cache-first for immutable job artifacts)
    // ---------------------------------------------------------------------
    function cachedFetch(url, opts) {
        if (!supported()) return fetch(url, opts);
        return caches.open(CACHE_NAME).then(function (cache) {
            return cache.match(url).then(function (hit) {
                if (hit) return hit;
                return fetch(url, opts).then(function (resp) {
                    if (resp && resp.ok) {
                        // Clone before the caller consumes the body.
                        cache.put(url, resp.clone()).catch(function () {});
                    }
                    return resp;
                });
            });
        }).catch(function () {
            return fetch(url, opts);
        });
    }

    // Store a single artifact URL into the cache if not already present and not
    // oversized. Returns a promise that always resolves.
    function cacheUrl(cache, url) {
        return cache.match(url).then(function (hit) {
            if (hit) return;
            return fetch(url, { credentials: 'include' }).then(function (resp) {
                if (!resp || !resp.ok) return;
                var len = parseInt(resp.headers.get('content-length') || '0', 10);
                if (len && len > MAX_ARTIFACT_BYTES) return;   // skip huge files
                return cache.put(url, resp.clone()).catch(function () {});
            });
        }).catch(function () { /* network/quota errors are non-fatal */ });
    }

    // ---------------------------------------------------------------------
    // Discover a job's artifact filenames via the API
    // ---------------------------------------------------------------------
    function discoverArtifacts(jobId) {
        return fetch('/api/jobs/' + encodeURIComponent(jobId) + '/artifacts', { credentials: 'include' })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (j) {
                if (!j || !Array.isArray(j.artifacts)) return [];
                return j.artifacts.map(function (a) { return a && a.filename; }).filter(Boolean);
            })
            .catch(function () { return []; });
    }

    // ---------------------------------------------------------------------
    // Purge all cached entries for a job
    // ---------------------------------------------------------------------
    function purgeJob(jobId) {
        if (!supported() || !jobId) return Promise.resolve();
        var prefixes = jobPathPrefixes(jobId);
        return caches.open(CACHE_NAME).then(function (cache) {
            return cache.keys().then(function (keys) {
                return Promise.all(keys.map(function (req) {
                    var path;
                    try { path = new URL(req.url).pathname; } catch (e) { return null; }
                    for (var p = 0; p < prefixes.length; p++) {
                        if (path.indexOf(prefixes[p]) === 0) return cache.delete(req);
                    }
                    return null;
                }));
            });
        }).catch(function () {});
    }

    // ---------------------------------------------------------------------
    // Prefetch ALL files for one job
    // ---------------------------------------------------------------------
    function prefetchJob(jobId, filenames) {
        if (!supported() || !prefetchAllowed() || !jobId) return Promise.resolve();

        // Record in LRU and purge whatever falls off the end.
        var evicted = touchJob(jobId);
        evicted.forEach(function (id) { purgeJob(id); });

        var ensureNames = (filenames && filenames.length)
            ? Promise.resolve(filenames)
            : discoverArtifacts(jobId);

        return ensureNames.then(function (names) {
            names = (names || []).filter(Boolean);
            if (!names.length) return;
            return caches.open(CACHE_NAME).then(function (cache) {
                var urls = names.map(function (fn) { return artifactUrl(jobId, fn); });
                return runQueue(urls, function (url) { return cacheUrl(cache, url); }, MAX_CONCURRENCY);
            });
        }).catch(function () {});
    }

    // ---------------------------------------------------------------------
    // Prefetch the most-recent N completed jobs
    // ---------------------------------------------------------------------
    function prefetchRecentJobs(limit) {
        if (!supported() || !prefetchAllowed()) return Promise.resolve();
        limit = limit || RECENT_LIMIT;
        return fetch('/api/jobs', { credentials: 'include' })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (jobs) {
                if (!Array.isArray(jobs)) return;
                // /api/jobs is ordered by batch first, not pure recency — sort
                // client-side by created_at/updated_at so "last 5" is truly recent.
                var recent = jobs
                    .filter(function (j) { return j && j.job_id && isCompletedStatus(j.status); })
                    .sort(function (a, b) {
                        return new Date(b.created_at || b.updated_at || 0) -
                               new Date(a.created_at || a.updated_at || 0);
                    })
                    .slice(0, limit);

                // Sequential across jobs (low priority) but parallel within a job.
                return recent.reduce(function (chain, job) {
                    return chain.then(function () { return prefetchJob(job.job_id); });
                }, Promise.resolve());
            })
            .catch(function () {});
    }

    function clearAll() {
        writeIndex([]);
        if (!supported()) return Promise.resolve();
        return caches.delete(CACHE_NAME).catch(function () {});
    }

    // ---------------------------------------------------------------------
    // Public API
    // ---------------------------------------------------------------------
    window.PhonosJobCache = {
        fetch: cachedFetch,
        prefetchJob: prefetchJob,
        prefetchRecentJobs: prefetchRecentJobs,
        purgeJob: purgeJob,
        clear: clearAll,
        supported: supported
    };

    // ---------------------------------------------------------------------
    // Auto-init: on any page that loads this script (dashboard + view_output,
    // both behind login), prefetch the recent jobs once the page is idle so it
    // never competes with the user's current load.
    // ---------------------------------------------------------------------
    if (supported()) {
        var kickoff = function () {
            onIdle(function () { prefetchRecentJobs(RECENT_LIMIT); }, 6000);
        };
        if (document.readyState === 'complete') {
            kickoff();
        } else {
            window.addEventListener('load', kickoff, { once: true });
        }
    }
})();
