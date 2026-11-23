---
status: accepted
---

# Flash transfers go through an outside QQ client service, and a message waits for them

For a streamer with flash transfers, the files of a processed archive are put on QQ's flash transfer (闪传), and its flash link replaces the short download link in 「全部处理完成」 and in the source record sheet. The upload is not done by this service. It is done by a separate service the owner runs beside it: a headless QQ client, logged in as a QQ account of the owner's (the flash account), with an unofficial automation layer that exposes the client's own flash transfer as actions over a WebSocket. This service only calls those actions. The owner chose this on 2026-11-23.

The reason is the download route. The editors are in mainland China and the server is a home connection in Hong Kong: a download from the dashboard crosses the border at whatever the route gives that evening, cannot be resumed and shows no size. A drive copy on Quark is only fast for an editor who pays for Quark. A flash transfer is served from Tencent's own storage to any QQ user, and the group already passed its files around that way by hand (the sheet's column is called 闪传链接).

QQ's official bot cannot make a flash transfer; only a user's client can. The automation layer is unofficial, uses a real account against QQ's terms, and is no longer maintained by its authors: when QQ stops accepting the client version it was made for, flash transfers end, and nobody is expected to repair it. The owner accepted that knowingly. The design therefore treats the flash transfer as something that may be gone one day without taking anything else with it.

## Considered options

- **Keep the short download link only.** Nothing new to run, but it is the slow route this decision is about.
- **Drive copies on Quark for everybody.** Already built. Fast only with a paid Quark membership on the editor's side.
- **An agent that operates the QQ desktop client's window** (screenshots and clicks). Rejected: slow, breaks with every change of the window, and needs a desktop that is never locked.
- **Implementing the flash transfer protocol without a QQ client.** Rejected: it means writing a QQ protocol client.
- **The automation layer inside this service or this repository.** Rejected: its licence does not allow changed code to be published, this repository is public, and a failure of it should not be able to stop the pipeline.

## Consequences

- Nothing of the outside service is in this repository: not its code, not its container, not what it is built from. The repository has a per-streamer switch, one address in `config.json`, and the names of the actions it calls. Without the address the feature is off and everything behaves as before.
- 「全部处理完成」 of a streamer with flash transfers waits for the upload, because it carries the flash link. When the upload fails for a reason other than the flash account (three tries, as the pipeline retries), the message goes out with the short download link instead and the owner is told once.
- When the flash account is offline, or the outside service cannot be reached (the two cannot be told apart from here), the flash transfer waits, up to 72 hours, and the owner is reminded on Discord, once and then once a day while a live waits. The 通知 page shows the account's state and, while it is offline, the QR code to scan. After 72 hours the message goes out with the short download link. The drive copy does not wait for any of this.
- A flash link expires after 14 days, by the owner's choice (QQ also offers 90 and 180 days). The archive's page shows it until then.
- The files go up as they are, not zipped: the video, the cleaned chat, both transcripts as subtitles and the highlight table. One file may be at most 10 GB (the owner's knowledge of QQ's limit, not measured here), so a bigger video is cut without re-encoding into parts that each play by themselves, named with their place in the live.
- An archive is not deleted from the server until its flash transfer is uploaded or given up on, as with a drive copy.
- The upload runs in the outside service, so a restart of this service does not interrupt it: the transfer's id is kept in the queue and its progress is asked again after the restart. A deploy does not wait for a flash transfer.
- The flash account does nothing else: it posts nothing in a group. The less it does, the less reason QQ has to restrict it.

Measured from the server (Hong Kong) on 2026-11-23:

- A 7.7 GB video was accepted as one file and uploaded in 5 minutes (26 MB/s on average, 34 at most). The same evening route gave a drive copy 0.3 to 1.6 MB/s.
- Creating a transfer answers at once with its id, its link and its expiry; the upload goes on afterwards, and its progress (bytes sent, files done, files failed) can be asked for.
- A client that was killed minutes after its first login had lost the login and asked for the QR code again. The outside service must be stopped gently, and a restart can cost a scan.
- Not measured: a file over 10 GB, a limit on the whole transfer's size, what a download looks like for an editor without a QQ membership, how long a login lasts.
