# 切片小助手

Watches streamers, downloads each finished live, transcribes and translates it, builds a 路灯表, and shows it all to the editors who cut clips from it.

## Language

### Lives

**Live**:
One continuous broadcast as its platform identifies it (一场直播): a YouTube video, a Twitch go-live, a TwitCasting movie. A streamer who drops and comes back may produce several lives in one sitting.
_Avoid_: stream, broadcast

**Sitting**:
One or more lives in a row by the same streamer that the group sees as one (一次开播): announced once when it starts and once when it ends. A new live continues the sitting when it starts at the same watch address shortly after the previous one stopped.
_Avoid_: show, run

**Singing live**:
A live whose title says the streamer will sing (歌回, 歌枠).
_Avoid_: karaoke stream, song stream, 歌枠 live

**Replay**:
The platform's own video of a finished live (回放). It may be unpublished, or need a login to watch.
_Avoid_: VOD, platform archive

### Archives

**Archive**:
One live's folder on disk and everything in it (录播).
_Avoid_: VOD folder, download, recording

**Artifact**:
One file of an archive: the video, raw chat, chat, transcript, Chinese transcript, highlight table or drive link.
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

**Song entry**:
One song sung in a singing live, as the highlight table shows it: when it was sung, its name and its original artist. A song nobody could name is still an entry, marked 未识别.
_Avoid_: setlist item, track

**Drive copy**:
What is kept of one archive on the cloud drive (网盘副本): one zip of its video, its chat, both transcripts as subtitles and its highlight table as a spreadsheet. It outlives the archive, which is deleted once its streamer has newer ones.
_Avoid_: final result, cloud backup

**Drive link**:
The address editors download a drive copy from (网盘链接), with its extraction code. It is noted in the archive, so the archive's page can show it.
_Avoid_: share link, pan link

**Signed download link**:
An address of the dashboard that downloads one archive's zip without logging in, until it expires. A message carries it for the people of a group, who have no account.
_Avoid_: public link, temporary link

### Group files

**Group file**:
A file a member posts in a QQ group (群文件), in the chat or through the group's file panel. The bot sees both the same way, as a message with the file attached.
_Avoid_: group attachment, QQ file

**Filing rule**:
Which group files are kept on the cloud drive and where: a tag in the file's name, an extension, and the subfolder they lead to.
_Avoid_: sort rule, routing rule

**Group folder**:
The folder on the cloud drive that holds one QQ group's filed files, named after the group.
_Avoid_: group directory

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
