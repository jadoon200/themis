/* Runs before the stylesheet paints, so a light-theme reader never sees a dark flash.
 * A separate file rather than an inline script: the content-security policy forbids those. */
(function () {
  try {
    var saved = window.localStorage.getItem("themis-theme");
    if (saved === "light" || saved === "dark") {
      document.documentElement.setAttribute("data-theme", saved);
    }
  } catch (e) {
    /* storage refused (private window, policy): the default theme stands */
  }
})();
