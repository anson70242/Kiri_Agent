---
status: accepted
---

# Group files reach the service through the official bot's message events

The owner wants the files a subtitle group posts in its QQ group kept on the cloud drive. QQ's official bot API has no call for a group's files: nothing lists them, reads their folders or fetches one later. What it does give is the message a file is posted in, at the moment it is posted, with the file attached. So a group file is taken from that message event, by the official bot the service already runs. The owner chose this on 2026-11-19.

A bot only gets the messages that mention it, unless the group's owner turns on "接收所有消息" for it in mobile QQ. With that on, QQ sends the bot every message of the group. The feature needs it, so in such a group the service receives the whole chat. It acts on two things only, a file attached to a message and a mention of the bot, and it neither stores nor logs anything else of a message.

## Considered options

- **A protocol client logged in as a personal QQ account** (the OneBot family: NapCat, LLBot, Lagrange). It can list a group's files and fetch any of them, old ones too. Rejected by the owner: it needs a real account on the server, the account can be restricted, and when this was decided the best-known projects were going away (NapCat's repository was emptied on 2026-11-12; another was closed after a lawyer's letter from Tencent, by secondary sources).
- **An MCP server for QQ.** None is official. Every one found that reads group files is a wrapper around those protocol clients and falls with them.

## Consequences

- Files already in a group are out of reach, and so is a file posted while the bot's connection or the service is down: there is no event to replay. A deploy's restart is such a gap.
- The event does not say which folder of the group's file panel a file was put in, and a file dropped in the chat looks the same as one uploaded in the panel. So a file is filed by its name alone (the filing rules).
- In a group that sends the bot everything, a mention no longer arrives as its own event: it is an ordinary message that lists the bot among the mentioned, marked as the bot itself. The gateway has to recognise a mention both ways.
- The file is fetched from an address in the event, on a host of QQ's, without a login. It is fetched at once: how long the address lives is not documented.

Seen on 2026-11-19 in the owner's group, with a personal-entity bot:

- "接收所有消息" needs no new intent and no approval from QQ.
- A file's attachment carries its content type (`file`), its name, its size and the address. The address answered range requests and served 560 MB in 17 seconds to the server in Hong Kong, and still worked an hour after posting.
- An ordinary message carries its author's role in the group, as a mention event does.
