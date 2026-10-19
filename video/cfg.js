// Target helpers for the AI Fluency Insights recording.
//   card:<h3 text>  -> the card with that heading in the visible tab
//   nav:<slug>      -> sidebar button (data-s)
(function () {
  // The plan-usage card needs a live Claude Code session; on sample data it only says "0% used".
  // The footer shows the local log path, which here is a sandbox folder.
  var css = '#planCard{display:none!important}footer{display:none!important}';
  var add = function () {
    var s = document.createElement('style');
    s.textContent = css;
    (document.head || document.documentElement).appendChild(s);
  };
  if (document.head || document.documentElement) add();
  else document.addEventListener('DOMContentLoaded', add);
})();

window.V_CONFIG = {
  root: 'section[id^="s-"]:not([hidden])',
  ready: '#kpis .kpi',
  resolvers: {
    card: function (want, root) {
      want = want.toLowerCase();
      return [].slice.call(root.querySelectorAll('.card')).find(function (c) {
        var h = c.querySelector('h3');
        return h && h.textContent.trim().toLowerCase() === want;
      });
    },
    nav: function (want) { return document.querySelector('#nav button[data-s="' + want + '"]'); }
  }
};
