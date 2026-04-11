(function () {
  if (window.__yflixDownloaderInstalled) {
    return;
  }
  window.__yflixDownloaderInstalled = true;

  function parseSeasonEpisode() {
    const hashMatch = location.hash.match(/(?:#|&)ep=(\d+),(\d+)/i);
    if (hashMatch) {
      return { season: hashMatch[1], episode: hashMatch[2] };
    }
    const text = [document.title, document.body.innerText || ""].join(" ");
    const compactMatch = text.match(/\bS(\d{1,2})E(\d{1,2})\b/i);
    if (compactMatch) {
      return { season: compactMatch[1], episode: compactMatch[2] };
    }
    const verboseMatch = text.match(/Season\s+(\d+)\D+Episode\s+(\d+)/i);
    if (verboseMatch) {
      return { season: verboseMatch[1], episode: verboseMatch[2] };
    }
    return { season: null, episode: null };
  }

  function stripEpisodeContext(value) {
    return String(value || "")
      .replace(/\s*[-|]\s*Y?Flix.*$/i, "")
      .replace(/(?:\s*[-:|]?\s*(?:Season\s*\d+|Episode\s*\d+|S\d{1,2}E\d{1,2}|Ep\.?\s*\d+).*)$/i, "")
      .replace(/\s+/g, " ")
      .trim();
  }

  function extractMetadata() {
    const pageText = document.body.innerText || "";
    const titleNode = document.querySelector("h1, .title, [data-title]");
    const rawTitle = (titleNode?.textContent || document.title || "Untitled").trim();
    const urlEpisode = parseSeasonEpisode();
    const yearMatch = [rawTitle, document.title, pageText].map((text) => text.match(/\b(19|20)\d{2}\b/)).find(Boolean);
    const episodeTitle = extractEpisodeTitle(pageText, urlEpisode.episode);
    const seriesName = stripEpisodeContext(rawTitle) || stripEpisodeContext(document.title) || rawTitle;
    return {
      raw_title: rawTitle,
      series_name: seriesName,
      year: yearMatch ? yearMatch[0] : null,
      season: urlEpisode.season,
      episode: urlEpisode.episode,
      episode_title: episodeTitle
    };
  }

  function extractEpisodeTitle(pageText, episodeNumber) {
    const selectors = [
      "[aria-current='true']",
      ".active",
      ".selected",
      "[class*='active']",
      "[class*='episode']"
    ];
    for (const selector of selectors) {
      const nodes = Array.from(document.querySelectorAll(selector));
      for (const node of nodes) {
        const text = (node.textContent || "").replace(/\s+/g, " ").trim();
        const derived = parseEpisodeTitle(text, episodeNumber);
        if (derived) {
          return derived;
        }
      }
    }
    return parseEpisodeTitle(pageText, episodeNumber);
  }

  function parseEpisodeTitle(text, episodeNumber) {
    const compact = (text || "").replace(/\s+/g, " ").trim();
    if (!compact) {
      return "";
    }
    if (episodeNumber) {
      const patterns = [
        new RegExp(`Episode\\s*${episodeNumber}\\s*[-:|]\\s*([^\\n]+)`, "i"),
        new RegExp(`E(?:pisode)?\\s*${episodeNumber}\\s*[-:|]\\s*([^\\n]+)`, "i"),
        new RegExp(`\\b${episodeNumber}\\b\\s*[-:|]\\s*([^\\n]+)`, "i")
      ];
      for (const pattern of patterns) {
        const match = compact.match(pattern);
        if (match && match[1]) {
          return match[1].trim();
        }
      }
    }
    return "";
  }

  function installNavigationGuards() {
    if (window.__yflixNavGuardInstalled) {
      return;
    }
    window.__yflixNavGuardInstalled = true;

    const originalOpen = window.open;
    window.open = function(url, target, features) {
      const href = String(url || "");
      if (!href) {
        return null;
      }
      if (href.startsWith("https://yflix.to/")) {
        location.href = href;
      }
      return null;
    };

    document.addEventListener("click", (event) => {
      const anchor = event.target instanceof Element ? event.target.closest("a") : null;
      if (!anchor) {
        return;
      }
      const href = anchor.href || "";
      if (!href || anchor.target !== "_blank") {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      if (href.startsWith("https://yflix.to/")) {
        location.href = href;
      }
    }, true);
  }

  function publishState() {
    chrome.runtime.sendMessage({
      type: "pageState",
      page_url: location.href,
      page_title: document.title,
      metadata: extractMetadata()
    }, () => void chrome.runtime.lastError);
  }

  function updatePulse() {
    if (!location.href.match(/^https:\/\/yflix\.to\/watch\//)) {
      return;
    }
    publishState();
  }

  function install() {
    installNavigationGuards();
    updatePulse();
    const observer = new MutationObserver(() => {
      if (!location.href.match(/^https:\/\/yflix\.to\/watch\//)) {
        return;
      }
      updatePulse();
    });
    observer.observe(document.documentElement, { childList: true, subtree: true });
    window.addEventListener("popstate", updatePulse);
    window.addEventListener("hashchange", updatePulse);
    setInterval(updatePulse, 4000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", install, { once: true });
  } else {
    install();
  }
})();
