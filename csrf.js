// Safe rendering helper for data that originated outside trusted markup.
window.escapeHtml = function (value) {
    return String(value == null ? '' : value)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
};

// Attaches the CSRF token (rendered into a <meta name="csrf-token"> tag by
// every authenticated page) to every state-changing fetch() call made from
// this page, so the app's JSON APIs are covered by CSRF protection the same
// way the HTML <form> posts are.
(function () {
    var metaTag = document.querySelector('meta[name="csrf-token"]');
    var token = metaTag ? metaTag.getAttribute('content') : null;
    if (!token) return;

    var unsafeMethods = ['POST', 'PUT', 'PATCH', 'DELETE'];
    var originalFetch = window.fetch;

    window.fetch = function (input, init) {
        init = init || {};
        var method = (init.method || 'GET').toUpperCase();

        if (unsafeMethods.indexOf(method) !== -1) {
            var headers = new Headers(init.headers || {});
            if (!headers.has('X-CSRFToken')) {
                headers.set('X-CSRFToken', token);
            }
            init.headers = headers;
        }

        return originalFetch(input, init);
    };
})();
