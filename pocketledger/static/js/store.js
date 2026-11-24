/* IndexedDB: the offline transaction queue and a cached copy of the entry-form options.
 * Queue rows are only ever deleted after the server has confirmed them (or the user discards them). */
(function () {
  'use strict';
  const PL = (window.PL = window.PL || {});
  const DB_NAME = 'pocketledger'; const DB_VERSION = 1;
  let dbPromise = null;

  function open() {
    if (dbPromise) return dbPromise;
    dbPromise = new Promise((resolve, reject) => {
      if (!window.indexedDB) { reject(new Error('IndexedDB is not available')); return; }
      const req = indexedDB.open(DB_NAME, DB_VERSION);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains('queue')) db.createObjectStore('queue', { keyPath: 'uuid' });
        if (!db.objectStoreNames.contains('meta')) db.createObjectStore('meta');
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => { dbPromise = null; reject(req.error); };
    });
    return dbPromise;
  }

  function run(store, mode, fn) {
    return open().then((db) => new Promise((resolve, reject) => {
      const t = db.transaction(store, mode); const s = t.objectStore(store);
      let out; const rq = fn(s);
      if (rq) rq.onsuccess = () => { out = rq.result; };
      t.oncomplete = () => resolve(out);
      t.onerror = () => reject(t.error); t.onabort = () => reject(t.error);
    }));
  }

  PL.store = {
    available: () => open().then(() => true, () => false),
    queueAll: () => run('queue', 'readonly', (s) => s.getAll()).then((a) => (a || []).sort((x, y) => (x.created_at < y.created_at ? -1 : 1))),
    queuePut: (item) => run('queue', 'readwrite', (s) => s.put(item)),
    queueDelete: (uuid) => run('queue', 'readwrite', (s) => s.delete(uuid)),
    queueGet: (uuid) => run('queue', 'readonly', (s) => s.get(uuid)),
    optionsGet: () => run('meta', 'readonly', (s) => s.get('options')),
    optionsPut: (o) => run('meta', 'readwrite', (s) => s.put(o, 'options')),
    clearOptions: () => run('meta', 'readwrite', (s) => s.delete('options')),
  };
})();
