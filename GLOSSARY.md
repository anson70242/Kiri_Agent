# 切片小助手

Watches streamers, downloads each finished live, transcribes and translates it, builds a 路灯表, and shows it all to the editors who cut clips from it.

## Language

### Lives

**Live**:
One continuous broadcast as its platform identifies it (一场直播): a YouTube video, a Twitch go-live, a TwitCasting movie. A streamer who drops and comes back may produce several lives in one sitting.
_Avoid_: stream, broadcast

**Replay**:
The platform's own video of a finished live (回放). It may be unpublished, or need a login to watch.
_Avoid_: VOD, platform archive

### Archives

**Archive**:
One live's folder on disk and everything in it (录播).
_Avoid_: VOD folder, download, recording

**Artifact**:
One file of an archive: the video, raw chat, chat, transcript, Chinese transcript or highlight table.
_Avoid_: output, sidecar

**Creator**:
The streamer name an archive is filed under (its top folder).
_Avoid_: channel, owner

**Transcript**:
The Japanese subtitles of an archive.
_Avoid_: Japanese transcript, ja subtitles

**Chinese transcript**:
The Chinese translation of an archive's transcript.
_Avoid_: zh transcript, translation

**Highlight table**:
The 路灯表: the scored table of an archive's segments that editors pick clips from.
_Avoid_: analysis, score sheet

### State files

**State file**:
A JSON file in the data directory holding state that survives restarts.
_Avoid_: store file, data file

**Operator data**:
A state file a person entered and that cannot be regenerated: the roster, the users, the QQ group subscriptions. When damaged, the service refuses to start.
_Avoid_: config, settings

**Runtime state**:
A state file the program generates as it runs: the queues, the live-stream state, the notification history, the sessions. When damaged, it is set aside as a backup, the owner is alerted, and the service continues empty.
_Avoid_: cache
