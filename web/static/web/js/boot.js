// Runs before the first paint, so the mobile menu starts collapsed only when
// the script that opens it is available too. Without JS the navigation stays open.
document.documentElement.classList.add('js');
