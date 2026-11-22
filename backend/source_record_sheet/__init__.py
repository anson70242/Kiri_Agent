"""The source record sheet (录播源记录表): a subtitle group's own sheet on Tencent Docs where
its editors look up where the files of a live can be fetched. The service adds a row to it
for an archive's short download link (录播), and another for its drive link (网盘).

    add(tab, row) --> SourceRecordKeeper --run()--> write_row, one row at a time

One concern a file:

- row.py      a row: its kind, its values, the text of each cell (the only place that knows them)
- writing.py  writing one row into one tab: headers, position, merges (the only place that knows them)
- worker.py   SourceRecordKeeper: takes rows in, writes them

The Tencent Docs calls themselves are backend/tencent_docs.py's (SheetTabCalls).
"""

from .row import HEADERS, LABELS, RowKind, SheetRow
from .worker import RowJob, SheetTab, SourceRecordKeeper
from .writing import MissingHeader, write_row

__all__ = ["HEADERS", "LABELS", "MissingHeader", "RowJob", "RowKind", "SheetRow", "SheetTab", "SourceRecordKeeper",
           "write_row"]
