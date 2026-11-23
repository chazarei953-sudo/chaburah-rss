# Chaburah: one combined podcast feed

The included Python program turns the **whole public [Chaburah shiurim library](https://chaburah.org/shiurim)** into **one RSS podcast subscription** for Podcast Addict. It is designed for free hosting with GitHub Pages.

- No separation by shiur series, rabbi or subject.
- A manual first run checks up to **600** recordings, enough for the **567** presently listed on the website.
- Scheduled updates run **four times a day**. Newly posted shiurim are included; older ones stay in the RSS archive.
- No hosting costs expected for a standard public GitHub Pages repository. You do **not** need to host the MP3s.
- The browser-based extractor uses the site's player and Download control to identify media links. The script verifies that enclosures respond as audio.

**Limitations:** The site/audio connection could not be exercised live from this workspace. Code and XML have passed offline tests, but a successful live GitHub workflow is still required. If media links are restricted, temporary, or blocked for third-party podcast clients, the extractor may need an adjustment or the source publisher's permission.

## Setup instructions

1. Sign in to [GitHub](https://github.com) or make a free account. Create a **public**, **empty** repository called `chaburah-rss`, using the default branch `main`.
2. Unzip the download and upload the **contents of the unzipped folder** into your repository, keeping the folder structure. Include `.github/workflows/podcast.yml` (this is the automatic updater). GitHub Desktop or a git push is the simplest way to retain hidden folders. On GitHub's web interface, you can also use **Add file → Upload files**, but check that `.github/workflows/podcast.yml` made it across.
3. In the repo, open **Settings → Pages → Build and deployment**. Choose **Source: GitHub Actions**.
4. Go to **Actions → Refresh Chaburah combined podcast RSS → Run workflow** and run it on `main`. A green check means it generated a feed and published GitHub Pages. If it is red, open the **Build the combined podcast feed** step and copy its log for troubleshooting.
5. Once the run is green, open `https://YOUR-GITHUB-USERNAME.github.io/chaburah-rss/feed.xml` (replace the name with your own). You should see XML with `<rss>` and `<item>` elements. Your status/landing page is `https://YOUR-GITHUB-USERNAME.github.io/chaburah-rss/`.
6. In Podcast Addict, tap **+**, select **RSS feed**, paste that complete `feed.xml` URL, and subscribe. You can turn on automatic downloads for this podcast if you want.

You don't need to repeat this process for new shiurim. GitHub automatically runs it at 01:27, 07:27, 13:27, and 19:27 UTC (GitHub schedules may be delayed).

### Alternative: upload with git

After creating the empty `chaburah-rss` repository:

```bash
git init
git add .
git commit -m "Install combined Chaburah RSS feed"
git branch -M main
git remote add origin https://github.com/YOUR-GITHUB-USERNAME/chaburah-rss.git
git push -u origin main
```

Do this **inside the unzipped folder**. You will need Git installed, plus normal GitHub authentication.

## If something goes wrong

- **No episodes / first run fails:** Send me the lines from the Action log that say `Found ... shiurim`, `FAILED ...`, and `Generated ... podcast episodes`. This will pinpoint the actual media URL format.
- **Podcast Addict gets episodes but can't play them:** An RSS feed only contains links. Some sites use access restrictions or changing signed audio URLs, which may need different handling.
- **Old episodes missing:** The first **manual** run tries the entire site. Regular checks process up to 85 outstanding episodes; the feed builds up if a batch could not finish previously.
- **Run succeeds but feed is not at the URL:** Confirm Pages is configured with **GitHub Actions** as its source. Also check the username and repository name.
- **GitHub asks for authorization:** You manage the repository, not ChatGPT. No password or token is built into the package.

## How it works

`build_feed.py` uses a headless Chromium browser to read the library. It tries HTML/JavaScript audio URLs, the audio player's media requests and the website's Download action. It probes the audio with an HTTP HEAD or tiny range request; it never intentionally saves the audio. It generates standards-based RSS `<enclosure>` elements using the original recording URLs, not their webpages. It records identified episodes in `public/episodes.json`, published alongside `feed.xml`, so subsequent runs can recover past results. The first run checks the full library; scheduled runs look for new and unresolved recordings. A recording is omitted if a usable audio link cannot be found.

The code is for personal listening, independent of Chaburah.org. Respect the source website's policies; do not redistribute recordings.

### Files

- `.github/workflows/podcast.yml` – automation and publishing
- `build_feed.py` – scraper and RSS generator
- `requirements.txt` – libraries
- `tests/test_build_feed.py` – offline checks
- `START_HERE.txt` – short installation instructions

Local test command:

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
python -m unittest discover -s tests -v
python build_feed.py --batch-size 5
```

If running outside GitHub, `GITHUB_REPOSITORY` or `FEED_URL` must be set to generate the correct advertised link.
