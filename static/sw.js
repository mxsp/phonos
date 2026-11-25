/**
 * Phonos Service Worker
 * Provides offline support, caching, and background sync
 */

const CACHE_VERSION = 'v2.3.1-18c5175d';
const STATIC_CACHE = `phonos-static-${CACHE_VERSION}`;
const DYNAMIC_CACHE = `phonos-dynamic-${CACHE_VERSION}`;
const API_CACHE = `phonos-api-${CACHE_VERSION}`;

// Assets to cache immediately on install
const STATIC_ASSETS = [
    '/',
    '/dashboard',
    '/static/css/app.css',
    '/static/js/app.js',
    '/static/fonts/inter.css',
    '/static/favicon.ico',
    '/offline'
];

// Install event - cache static assets
self.addEventListener('install', (event) => {
    console.log('[SW] Installing service worker...');
    event.waitUntil(
        caches.open(STATIC_CACHE)
            .then((cache) => {
                console.log('[SW] Caching static assets');
                return cache.addAll(STATIC_ASSETS.filter(url => !url.includes('undefined')));
            })
            .catch(err => console.log('[SW] Cache addAll error:', err))
            .then(() => self.skipWaiting())
    );
});

// Activate event - clean up old caches
self.addEventListener('activate', (event) => {
    console.log('[SW] Activating service worker...');
    event.waitUntil(
        caches.keys()
            .then((cacheNames) => {
                return Promise.all(
                    cacheNames
                        .filter((name) => name.startsWith('phonos-') &&
                            ![STATIC_CACHE, DYNAMIC_CACHE, API_CACHE].includes(name))
                        .map((name) => {
                            console.log('[SW] Deleting old cache:', name);
                            return caches.delete(name);
                        })
                );
            })
            .then(() => self.clients.claim())
    );
});

// Fetch event - serve from cache or network
self.addEventListener('fetch', (event) => {
    const { request } = event;
    const url = new URL(request.url);

    // Skip non-GET requests
    if (request.method !== 'GET') return;

    // Skip WebSocket connections
    if (url.protocol === 'ws:' || url.protocol === 'wss:') return;

    // Skip chrome-extension and other non-http(s) protocols
    if (!url.protocol.startsWith('http')) return;

    // API requests - Network first with 3s timeout, fall back to cache
    // This gives fresh data when online but still works offline
    if (url.pathname.startsWith('/api/')) {
        event.respondWith(networkFirstWithTimeout(request, API_CACHE, 3000));
        return;
    }

    // Always refresh critical bundles to avoid stale runtime errors after deploys.
    // Use stale-while-revalidate: instant load from cache, update in background.
    if (url.pathname === '/static/js/app.js' || url.pathname === '/static/css/app.css') {
        event.respondWith(staleWhileRevalidate(request, STATIC_CACHE));
        return;
    }

    // Locale files - Network first so newly-added translation keys show up
    // immediately. cacheFirst would pin the first en.json/de.json forever (it
    // ignores the `cache: 'no-cache'` the i18n loader sends), making any key
    // added later render as its raw "section.key" (e.g. "dashboard.pageTitle"
    // in the browser tab). Cached copy is still used as an offline fallback.
    if (url.pathname.startsWith('/static/locales/')) {
        event.respondWith(networkFirst(request, STATIC_CACHE));
        return;
    }

    // Other static assets - Cache first
    if (url.pathname.startsWith('/static/') || STATIC_ASSETS.includes(url.pathname)) {
        event.respondWith(cacheFirst(request, STATIC_CACHE));
        return;
    }

    // HTML pages - Network first with offline fallback
    if (request.headers.get('accept')?.includes('text/html')) {
        event.respondWith(networkFirstWithOffline(request));
        return;
    }

    // Everything else - Stale while revalidate
    event.respondWith(staleWhileRevalidate(request, DYNAMIC_CACHE));
});

// Cache strategies
async function cacheFirst(request, cacheName) {
    try {
        const cached = await caches.match(request);
        if (cached) return cached;

        const response = await fetch(request);
        if (response.ok) {
            const cache = await caches.open(cacheName);
            cache.put(request, response.clone());
        }
        return response;
    } catch (error) {
        console.error('[SW] Cache first failed:', error);
        // Return offline page for navigation requests
        if (request.headers.get('accept')?.includes('text/html')) {
            return caches.match('/offline');
        }
        throw error;
    }
}

async function networkFirst(request, cacheName) {
    try {
        const response = await fetch(request);
        if (response.ok) {
            const cache = await caches.open(cacheName);
            cache.put(request, response.clone());
        }
        return response;
    } catch (error) {
        const cached = await caches.match(request);
        if (cached) return cached;
        throw error;
    }
}

async function networkFirstWithTimeout(request, cacheName, timeoutMs = 3000) {
    const timeoutPromise = new Promise((_, reject) =>
        setTimeout(() => reject(new Error('timeout')), timeoutMs)
    );

    try {
        const response = await Promise.race([
            fetch(request),
            timeoutPromise
        ]);
        if (response.ok) {
            const cache = await caches.open(cacheName);
            cache.put(request, response.clone());
        }
        return response;
    } catch {
        const cached = await caches.match(request);
        if (cached) return cached;

        // Timeout but no cache — still try to complete the network request
        if (cached === undefined) {
            try {
                return await fetch(request);
            } catch {
                throw new Error('offline');
            }
        }
        throw new Error('offline');
    }
}

async function networkFirstWithOffline(request) {
    try {
        const response = await fetch(request);
        if (response.ok) {
            const cache = await caches.open(DYNAMIC_CACHE);
            cache.put(request, response.clone());
        }
        return response;
    } catch (error) {
        const cached = await caches.match(request);
        if (cached) return cached;

        // Return offline page
        return caches.match('/offline');
    }
}

async function staleWhileRevalidate(request, cacheName) {
    const cached = await caches.match(request);

    const fetchPromise = fetch(request)
        .then((response) => {
            if (response.ok) {
                caches.open(cacheName)
                    .then((cache) => cache.put(request, response.clone()));
            }
            return response;
        })
        .catch(() => cached);

    return cached || fetchPromise;
}

// Background sync for job submissions
self.addEventListener('sync', (event) => {
    if (event.tag === 'sync-jobs') {
        event.waitUntil(syncPendingJobs());
    }
});

async function syncPendingJobs() {
    // This would sync pending jobs from IndexedDB when online
    console.log('[SW] Syncing pending jobs...');
}

// Push notifications (for job completion)
self.addEventListener('push', (event) => {
    if (!event.data) return;

    try {
        const data = event.data.json();
        const options = {
            body: data.body || 'You have a new notification',
            icon: '/static/img/icon-192x192.png',
            badge: '/static/img/icon-72x72.png',
            vibrate: [100, 50, 100],
            data: data.data || {},
            actions: [
                { action: 'view', title: 'View' },
                { action: 'dismiss', title: 'Dismiss' }
            ]
        };

        event.waitUntil(
            self.registration.showNotification(data.title || 'Phonos', options)
        );
    } catch (error) {
        console.error('[SW] Push notification error:', error);
    }
});

self.addEventListener('notificationclick', (event) => {
    event.notification.close();

    if (event.action === 'view' && event.notification.data?.url) {
        event.waitUntil(
            clients.openWindow(event.notification.data.url)
        );
    } else if (event.action !== 'dismiss') {
        // Default action - open dashboard
        event.waitUntil(
            clients.openWindow('/dashboard')
        );
    }
});
