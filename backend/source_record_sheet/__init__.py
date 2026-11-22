"""The source record sheet (录播源记录表): a subtitle group's own sheet on Tencent Docs where
its editors look up where the files of a live can be fetched. The service adds a row to it
for an archive's short download link (录播), and another for its drive link (网盘).

    pipeline --processed(streamer, ...)--> SourceRecordKeeper --QQGroups.recording--> one job per tab
             (saved in the jobs file, unless the record says that row was written) --run()--> write_row,
             one row at a time

One concern a file:

- row.py      a row: its kind, its values, the text of each cell (the only place that knows them)
- writing.py  writing one row into one tab: headers, position, merges (the only place that knows them)
- jobs.py     the rows waiting (the jobs file, `source_record_jobs.json`): the only place a link is kept
- record.py   the rows written (`source_record_rows.json`): each row of a live goes into a tab once
- worker.py   SourceRecordKeeper: is told about lives, finds the tabs, writes the rows

The Tencent Docs calls themselves are backend/tencent_docs.py's (SheetTabCalls).
"""

from .row import HEADERS, LABELS, RowKind, SheetRow
from .jobs import RowJob, RowJobs, SheetTab, state_files
from .record import RowKey, RowRecord
from .worker import SourceRecordKeeper
from .writing import MissingHeader, write_row

__all__ = ["HEADERS", "LABELS", "MissingHeader", "RowJob", "RowJobs", "RowKey", "RowKind", "RowRecord", "SheetRow",
           "SheetTab", "SourceRecordKeeper", "state_files", "write_row"]
