# WriterAgent - AI Writing Assistant for LibreOffice
# Copyright (c) 2024 John Balis
# Copyright (c) 2026 KeithCu (modifications and relicensing)
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""Writer module — tools for Writer document manipulation."""

from plugin.framework.module_base import ModuleBase


class WriterModule(ModuleBase):
    """Registers Writer tools for outline, content, comments, styles, etc."""

    def initialize(self, services):
        self.services = services

        # Initialize core Writer services (merged from writer_nav and writer_index)
        from .bookmarks import BookmarkService
        from .tree import TreeService
        from .proximity import ProximityService
        from .index import IndexService

        doc_svc = services.document
        events = services.events

        bm = BookmarkService(doc_svc, events)
        tree = TreeService(doc_svc, bm, events)
        prox = ProximityService(doc_svc, tree, bm, events)
        idx = IndexService(doc_svc, tree, bm, events)

        services.register("writer_bookmarks", bm)
        services.register("writer_tree", tree)
        services.register("writer_proximity", prox)
        services.register("writer_index", idx)

        # Register tools
        from .outline import GetDocumentTree
        from .styles import ListStyles, GetStyleInfo
        from .images import GenerateImage
        from .content import GetDocumentContent, ApplyDocumentContent, ReadParagraphs, InsertAtParagraph, ModifyParagraph, DeleteParagraph, DuplicateParagraph, CloneHeadingBlock, InsertParagraphsBatch, GetDocumentStats
        from .search import SearchInDocument, GetIndexStats
        from .comments import ListComments, AddComment, DeleteComment
        from .tracking import SetTrackChanges, GetTrackedChanges, ManageTrackedChanges
        from .frames import ListTextFrames, GetTextFrameInfo, SetTextFrameProperties

        tools = [
            GetDocumentTree(),
            ListStyles(),
            GetStyleInfo(),
            GenerateImage(),
            GetDocumentContent(),
            ApplyDocumentContent(),
            ReadParagraphs(),
            InsertAtParagraph(),
            ModifyParagraph(),
            DeleteParagraph(),
            DuplicateParagraph(),
            CloneHeadingBlock(),
            InsertParagraphsBatch(),
            GetDocumentStats(),
            SearchInDocument(),
            GetIndexStats(),
            ListComments(),
            AddComment(),
            DeleteComment(),
            # Table tools: uncomment classes in tables.py, then import/register here.
            # ReplaceSection / FindParagraphForRange / FindTableForRange: add to structural.py when needed.
            SetTrackChanges(),
            GetTrackedChanges(),
            ManageTrackedChanges(),
            ListTextFrames(),
            GetTextFrameInfo(),
            SetTextFrameProperties(),
        ]
        services.tools.register_many(tools)
