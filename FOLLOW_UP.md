# Follow-up after the initial verification audit

Issue tracker status: issue creation through the connected GitHub integration was rejected with HTTP 403. This versioned record preserves the findings and acceptance criteria until an issue can be submitted. A prepared issue link is provided in the main README.

## When to revisit

Revisit after the initial verification-v5 audit completes. Keep this issue open until the affected titles are retested against fresh results. An unfinished preparation checkpoint is neither a pass nor a confirmed source failure.

Baseline: Jellyfin 10.11.11, Dynamic Library 1.4.0.19; [investigation and metadata fixes](https://github.com/<GITHUB_OWNER>/home-media-server/commit/a9b6978d7f69b5f7d6a46fe197e5c31e3d1cf8aa). The automated suite passed 116 tests, but physical TV retesting is still required.

## Confirmed defect: embedded Hebrew subtitle extraction

**56 Days, S1E1 — IMDb tt30412869**

- “עברית 1” is an embedded SubRip track, stream index 16. Selecting it failed.
- “עברית 2” is a separately downloaded Hebrew subtitle and worked.
- Native Jellyfin logs showed a missing extracted 16.srt file and a NullReferenceException in SubtitleEncoder.ExtractAllExtractableSubtitles. Related media-info/input-argument stack frames were present.
- This is a delivery/extraction defect, not evidence that only one Hebrew translation exists. The exact root cause remains unconfirmed.

Investigate native media-source identity, input/protocol resolution, and extraction/cache handling. Do not assume adding another subtitle provider fixes this path. Do not publish raw logs containing signed media URLs.

## Playback retest: Thamma

**IMDb tt28102562**

At the time of the report, the title still had an older v4 proof. Logs showed received video data, partial decoded progress, preparation timeouts, and resumable checkpoints; they did not establish a codec failure.

Cached metadata used the working title **Vampires of Vijay Nagar**, while the local IMDb index used **Thamma**. Background source matching now accepts current/original names and English aliases for the same IMDb identity while retaining year checks. Its audit was prioritized.

After fresh verification, establish whether remaining failure is source availability/progress, decoding, audio conversion, or the native client preparation path. Record sanitized phase/reason/progress evidence rather than guessing.

## Metadata regression check: Money Trap and similar blank shows

**IMDb tt38732131**

Cached metadata contained the name and eight released episodes, but the native entry showed only its IMDb number and no episodes. Recovery created episode metadata, notified Jellyfin of the specific directory, and updated the bare parent through the supported metadata API.

Verified after repair: correct native name, eight indexed episodes, and S1E1 visible. Other episodes were still awaiting source verification. Similar existing placeholders are repaired in bounded background batches. Recheck that names and verified episodes remain available after the audit and ordinary syncs.

## Distinguish subtitle coverage from extraction failures

King of Dope (tt14330326) had no embedded subtitles and zero cached Wizdom matches during the test. Anna Pigeon S1E1 (tt36937503) also had zero cached Wizdom matches. These coverage observations are separate from the confirmed embedded-track failure above; no new provider was added.

## Retest and closure checklist

- [ ] Review completed fresh v5 outcomes for the affected movie/episodes; retain failed results and retry history.
- [ ] Reproduce and fix embedded Hebrew extraction with a regression test; do not advertise a broken track as usable.
- [ ] On the TV, verify Hebrew 1 → Hebrew 2 → None → Hebrew 1, Hebrew menu labels, and subtitle delivery after seeking.
- [ ] Retest Thamma start, sound, and seeking on TV and phone; explain any remaining failure with actual evidence.
- [ ] Verify Money Trap and another previously blank series show names, seasons, and verified playable episodes.
- [ ] Preserve watch position, favorites, metadata identity, saved media, source history, and active viewing holds.
- [ ] Protect ongoing/paused viewing. Do not restart playback services based solely on an empty session response when viewing is known to be active.
- [ ] Keep public evidence sanitized: no credentials, private endpoints, native user/item identifiers, signed links, or raw runtime databases.

No subtitle-provider expansion or playback-service restart was applied during the initial investigation.
