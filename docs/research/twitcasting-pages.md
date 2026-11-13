# TwitCasting pages: what was verified, and how to verify again

Read this when TwitCasting monitoring, replay download or chat stops working, or before changing how those pages are parsed. It records what was checked against the real site on 2026-11-13, which parts were never seen, and how to capture fresh pages. The parsers are the source of truth for the markers in use; this file is the source of truth for *why* those markers and for what is still a guess.

TwitCasting has no stable public contract for these pages. Everything here is observation.

## Capturing fresh pages

- Use a desktop browser User-Agent. `Accept-Language` made no difference: pages came back as `<html lang="en">`; with login cookies the language follows the account.
- **A channel that is live right now**: the front page `https://twitcasting.tv/` carries `data-userid="<id>"` on its thumbnails. Ask the stream server about each id until one answers live. The on-request network test picks its live channel this way.
- **An offline channel**: the site's own account `twitcasting_jp` is normally offline.
- **Replay pages need login cookies** (a Netscape cookies file). Fetch with the cookies for the published and the not-published states, and without them for the login-missing state.
- Captured pages hold viewers' names and comments, and logged-in pages hold account details. Keep captures outside the repository. Fixtures are small hand-made pages with invented names that keep only the markers.

## Channel page `https://twitcasting.tv/<id>` (no login)

| Marker | Live | Offline |
| --- | --- | --- |
| `<body data-is-onlive="...">` | `"true"` | `"false"` |
| `data-movie-id` | the live's movie id | the latest movie's id |
| `data-live-type` | `"live"` | `"movie"` |
| `data-started-at` (milliseconds) | the real start time | **not real**: now minus the last movie's duration |
| `data-broadcaster-profile-image` | avatar | avatar |
| `og:image` | cover | cover of the latest movie |

- An unknown id answers HTTP 200 with the site's front page, which has no `data-is-onlive`. A page without the marker is an error, never offline.
- `https://twitcasting.tv/streamserver.php?target=<id>&mode=client&player=pc_web` returns `{"movie":{"id":<int>,"live":<bool>}, ...}`, and `{}` with HTTP 200 for an unknown id.
- `POST https://frontendapi.twitcasting.tv/watch/user/<id>` returns `current_count`, but it was 0 on a live channel: not a viewer count. The page has no viewer count either.

## Titles

A channel has a fixed channel title; a live may have its own title.

- `og:title` is `<live title> / <channel title>` when the live has its own title, and just `<channel title>` when it has none.
- `og:description` equals the live's title when there is one, and holds unrelated text when there is none. It is not usable as the title.
- `<h2 id="player-title">` is the channel title on a channel page and the full `og:title` on a replay page.
- The rule in use: take `og:title` and drop the last ` / ` and what follows; keep it whole when there is no ` / `. A live title that itself contains ` / ` on a channel without a channel-title suffix would be cut short; no such case was seen.
- yt-dlp's `title` for a replay is the full `og:title`.

## Replay page `https://twitcasting.tv/<id>/movie/<movie id>`

Every replay on the site needs a login (checked on three unrelated channels). Without cookies yt-dlp fails with `Failed to get m3u8 playlist`, also for published replays.

| State | Marker | Seen |
| --- | --- | --- |
| Published | the page has `data-movie-playlist='...'` (only when logged in) | yes |
| Login missing or expired | `data-account-type="not_logged_in"`, no playlist; text "Login required to watch" | yes |
| Not published | `data-account-type=""`, no playlist, a `tw-player-empty-message` element; text "This video is not published" | yes |
| Password-protected | a `<form method="POST">` holding `<input name="password">` | no: taken from yt-dlp's source |
| Membership-only | unknown; may look like "not published" | no |

- Classify by markers, not by the message text: the text follows the account's language.
- Start time: `<time data-toggle="true" datetime="Tue, 06 Oct 2026 01:30:44 +0900">` (RFC 2822). yt-dlp reads the same element as `timestamp`.
- With cookies, yt-dlp gives one HLS format (1080p H.264 with AAC) for a published replay. A replay of 2 h 44 min was a single file.
- A replay in several parts comes back from yt-dlp as `multi_video` with entries `<id>-0`, `<id>-1`, and each entry repeats the whole replay's metadata, duration included. No real one was found; the joining was run only on generated clips.
- yt-dlp writes refreshed cookies back to the cookies file it is given.
- Checked from two networks (a PC in Japan, the server in Hong Kong): the same cookies worked from both, and using them from the second did not end the login on the first.

## Comment history `https://twitcasting.tv/<id>/moviecomment/<movie id>` (no login)

- Readable without login, also for a replay that is not published.
- 20 comments a page, newest first; one real page held 19. The bare address and `...-0` are the first page, `...-1` the next older one. The pager links to the last page index.
- One comment: a `<div class="tw-comment-history-item" data-comment-id="...">` holding the text in `tw-comment-history-item__content__text`, the commenter in an `<a class="tw-comment-history-item__details__user-link" href="/<user id>">`, and the time in a `<time datetime="...">` (RFC 2822).
- The page's comment count matched what the walk collected (432 of 432 on one movie).
- Offset in the video is comment time minus the movie's start time; the first comment of one movie sat at second 1. Comments keep arriving after the live ends: 49 of 432 on that movie were later than the video's end.
- Never seen: the page of a movie with no comments, and how long a movie's history stays available.

## The official API (not used)

Read from the API v2 documentation, never called. It has no replay video: the movie object's `hls_url` is the live stream. It offers live state, `is_recorded` (replay published), `is_protected` (password), `is_membership`, and comments at 50 a call, 60 calls a minute. It needs a registered application (ClientID and ClientSecret). `is_membership` is the one field that would settle the membership-only state above.
