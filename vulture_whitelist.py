# Whitelist for Vulture dead code analyzer.
#
# Keeps only genuinely used symbols that Vulture cannot trace statically:
#   1. UNO callback/listener methods and pyuno interface implementations
#   2. Registry-registered tools and tool-schema fields
#   3. Dynamic dispatch functions and ServiceRegistry names
#   4. Test hooks, test fixtures, and code called only by tests
#
# Rule: Do not delete code or tests; skip writeragent_api.py.


class _WhitelistMock:
    """Mock object that dynamically resolves any attribute access safely."""

    def __getattr__(self, name: str) -> "_WhitelistMock":
        return self


_ = _mock = _WhitelistMock()


# =============================================================================
# 1. UNO Callbacks, Listeners, and pyuno Interface Implementations
#
# Methods invoked directly by LibreOffice C++ runtime through PyUNO event
# listener and dispatch interfaces. Vulture cannot trace these callers
# because the invocations originate outside the Python codebase.
# =============================================================================
_.actionPerformed  # method: plugin/calc/python/cell_editor_ui.py:240
_.activeSpreadsheetChanged  # method: plugin/framework/uno_listeners.py:553
_.addLinguServiceEventListener  # method: plugin/writer/locale/ai_grammar_proofreader.py:1057
_.addStatusListener  # method: plugin/main.py:1197
_.createAccessible  # method: plugin/chatbot/panel_factory.py:269
_.createUIElement  # method: plugin/chatbot/panel_factory.py:1401
_.detect  # method: plugin/notebook/import_filter.py:74
_.doProofreading  # method: plugin/writer/locale/ai_grammar_proofreader.py:865
_.documentEventOccured  # method: plugin/doc/document_helpers.py:513
_.elementInserted  # method: plugin/framework/uno_listeners.py:390
_.elementRemoved  # method: plugin/framework/uno_listeners.py:394
_.elementReplaced  # method: plugin/framework/uno_listeners.py:398
_.focusGained  # method: plugin/framework/uno_listeners.py:592
_.focusLost  # method: plugin/framework/uno_listeners.py:596
_.getArgumentDescription  # method: plugin/calc/addin_common.py:72
_.getDisplayCategoryName  # method: plugin/calc/addin_common.py:94
_.getDisplayFunctionName  # method: plugin/calc/addin_common.py:64
_.getFunctionDescription  # method: plugin/calc/addin_common.py:68
_.getHeightForWidth  # method: plugin/chatbot/panel_factory.py:272
_.getLocale  # method: plugin/calc/addin_common.py:97
_.getLocales  # method: plugin/writer/locale/ai_grammar_proofreader.py:678
_.getProgrammaticCategoryName  # method: plugin/calc/addin_common.py:91
_.getProgrammaticFunctionName  # method: plugin/calc/addin_common.py:60
_.getRealInterface  # method: plugin/chatbot/panel_factory.py:504
_.getServiceDisplayName  # method: plugin/writer/locale/ai_grammar_proofreader.py:1134
_.getTransferData  # method: plugin/chatbot/dialogs.py:427
_.getTransferDataFlavors  # method: plugin/chatbot/dialogs.py:430
_.getWindow  # method: plugin/chatbot/panel_factory.py:266
_.hasLocale  # method: plugin/writer/locale/ai_grammar_proofreader.py:673
_.ignoreRule  # method: plugin/writer/locale/ai_grammar_proofreader.py:1033
_.isDataFlavorSupported  # method: plugin/chatbot/dialogs.py:437
_.isSpellChecker  # method: plugin/writer/locale/ai_grammar_proofreader.py:862
_.itemStateChanged  # method: plugin/calc/python/cell_editor_ui.py:259
_.keyPressed  # method: plugin/chatbot/slash_popup.py:504
_.keyReleased  # method: plugin/chatbot/slash_popup.py:511
_.mouseEntered  # method: plugin/calc/navigation.py:317
_.mouseExited  # method: plugin/calc/navigation.py:320
_.mousePressed  # method: plugin/calc/navigation.py:295
_.mouseReleased  # method: plugin/calc/navigation.py:298
_.notifyContextMenuExecute  # method: plugin/calc/python/editor_context_menu.py:77
_.queryDispatches  # method: plugin/main.py:1163
_.queryTermination  # method: plugin/main.py:139
_.removeLinguServiceEventListener  # method: plugin/writer/locale/ai_grammar_proofreader.py:1067
_.removeStatusListener  # method: plugin/main.py:1227
_.resetIgnoreRules  # method: plugin/writer/locale/ai_grammar_proofreader.py:1045
_.setLocale  # method: plugin/calc/addin_common.py:100
_.setTargetDocument  # method: plugin/notebook/import_filter.py:88
_.textChanged  # method: plugin/calc/python/cell_editor_ui.py:267
_.trigger  # method: plugin/main.py:1070
_.windowActivated  # method: plugin/framework/uno_listeners.py:506
_.windowClosed  # method: plugin/framework/uno_listeners.py:494
_.windowClosing  # method: plugin/framework/uno_listeners.py:490
_.windowDeactivated  # method: plugin/framework/uno_listeners.py:510
_.windowHidden  # method: plugin/framework/uno_listeners.py:468
_.windowMinimized  # method: plugin/framework/uno_listeners.py:498
_.windowMoved  # method: plugin/framework/uno_listeners.py:460
_.windowNormalized  # method: plugin/framework/uno_listeners.py:502
_.windowOpened  # method: plugin/framework/uno_listeners.py:486
_.windowResized  # method: plugin/framework/uno_listeners.py:456
_.windowShown  # method: plugin/framework/uno_listeners.py:464

# =============================================================================
# 2. Registry-Registered Tools and Tool Schemas
#
# Tool classes (ToolBase subclasses) discovered dynamically by package
# auto-discovery / tool registries, and tool schema fields/flags inspected
# via reflection during LLM dispatch. Vulture cannot trace reflective tool
# invocation from static Python ASTs.
# =============================================================================
_.AddCellComment  # class: plugin/calc/comments.py:119
_.AddComment  # class: plugin/writer/specialized/comments.py:87
_.AddConditionalFormat  # class: plugin/calc/conditional.py:131
_.AddSlide  # class: plugin/draw/pages.py:35
_.AdvancedSearch  # class: plugin/writer/search.py:1123
_.AlignShapes  # class: plugin/draw/shapes.py:856
_.AnalyzeDataTool  # class: plugin/calc/analysis.py:330
_.ApplyDesign  # class: plugin/draw/designs.py:1347
_.ApplyDocumentContent  # class: plugin/writer/apply_document_content.py:215
_.ApplyPptMasterNativeEnhance  # class: plugin/ppt_master/tools.py:98
_.ApplyPptMasterTemplateFill  # class: plugin/ppt_master/tools.py:78
_.ApplySheetFilter  # class: plugin/calc/sheet_filter.py:168
_.ApplyStyle  # class: plugin/writer/styles.py:897
_.BookmarkCleanup  # class: plugin/writer/specialized/bookmarks.py:557
_.BookmarkCreate  # class: plugin/writer/specialized/bookmarks.py:598
_.BookmarkDelete  # class: plugin/writer/specialized/bookmarks.py:633
_.BookmarkGet  # class: plugin/writer/specialized/bookmarks.py:689
_.BookmarkList  # class: plugin/writer/specialized/bookmarks.py:534
_.BookmarkRename  # class: plugin/writer/specialized/bookmarks.py:659
_.BookmarkResolve  # class: plugin/writer/specialized/bookmarks.py:715
_.BrainstormResearchWeb  # class: plugin/chatbot/brainstorming.py:74
_.BrainstormingSessionTool  # class: plugin/chatbot/brainstorming.py:153
_.ClearSheetFilter  # class: plugin/calc/sheet_filter.py:210
_.CloneHeadingBlock  # class: plugin/writer/structural.py:359
_.Command  # attribute: plugin/writer/specialized/mail_merge.py:537
_.CommandType  # attribute: plugin/writer/specialized/mail_merge.py:536
_.CommentCheckStop  # class: plugin/writer/specialized/comments.py:558
_.CommentDelete  # class: plugin/writer/specialized/comments.py:221
_.CommentList  # class: plugin/writer/specialized/comments.py:44
_.CommentResolve  # class: plugin/writer/specialized/comments.py:313
_.CommentScanTasks  # class: plugin/writer/specialized/comments.py:510
_.CommentWorkflowGet  # class: plugin/writer/specialized/comments.py:529
_.CommentWorkflowSet  # class: plugin/writer/specialized/comments.py:540
_.Constraints  # attribute: plugin/calc/analysis.py:271
_.ConvertSpreadsheetToPython  # class: plugin/calc/spreadsheet_import/import_tool.py:17
_.CreateDiagram  # class: plugin/draw/shapes.py:935
_.CreatePivotTable  # class: plugin/calc/pivot.py:126
_.CreateSheet  # class: plugin/calc/sheets.py:78
_.DataSourceName  # attribute: plugin/writer/specialized/mail_merge.py:535
_.DeepResearchSessionTool  # class: plugin/chatbot/deep_research_session.py:88
_.DeepResearchWebTool  # class: plugin/chatbot/deep_research_session.py:55
_.DelegateReadDocument  # class: plugin/doc/document_research_specialized.py:109
_.DelegateToSpecializedCalc  # class: plugin/calc/specialized.py:31
_.DelegateToSpecializedDraw  # class: plugin/draw/specialized.py:31
_.DelegateToSpecializedWriter  # class: plugin/writer/specialized_base.py:115
_.DelegateToolDomains  # class: plugin/doc/python_domain_specialized.py:382
_.DeleteCellComment  # class: plugin/calc/comments.py:164
_.DeleteSheet  # class: plugin/calc/sheets.py:134
_.DeleteSlide  # class: plugin/draw/pages.py:134
_.DeleteStructure  # class: plugin/calc/cells.py:973
_.DetectErrors  # class: plugin/calc/errors.py:38
_.DistributeShapes  # class: plugin/draw/shapes.py:896
_.DocumentURL  # attribute: plugin/writer/specialized/mail_merge.py:534
_.DuplicateSlide  # class: plugin/draw/pages.py:304
_.EmbeddedEdit  # class: plugin/writer/specialized/embedded.py:43
_.EmbeddedInsert  # class: plugin/writer/specialized/embedded.py:18
_.EvaluateFormula  # class: plugin/calc/formulas.py:163
_.ExportPresentationProject  # class: plugin/ppt_master/tools.py:24
_.FieldsDelete  # class: plugin/writer/specialized/fields.py:161
_.FieldsInsert  # class: plugin/writer/specialized/fields.py:273
_.FieldsList  # class: plugin/writer/specialized/fields.py:108
_.FieldsUpdateAll  # class: plugin/writer/specialized/fields.py:87
_.FileNameFromColumn  # attribute: plugin/writer/specialized/mail_merge.py:549
_.FileNamePrefix  # attribute: plugin/writer/specialized/mail_merge.py:548
_.FillDrawFields  # class: plugin/draw/field_fill.py:142
_.FindTools  # class: plugin/doc/find_tools_tool.py:126
_.FootnotesDelete  # class: plugin/writer/specialized/footnotes.py:234
_.FootnotesEdit  # class: plugin/writer/specialized/footnotes.py:202
_.FootnotesInsert  # class: plugin/writer/specialized/footnotes.py:87
_.FootnotesList  # class: plugin/writer/specialized/footnotes.py:175
_.FootnotesSettingsGet  # class: plugin/writer/specialized/footnotes.py:251
_.FootnotesSettingsUpdate  # class: plugin/writer/specialized/footnotes.py:289
_.ForecastDataTool  # class: plugin/calc/forecast.py:21
_.FormCreate  # class: plugin/writer/specialized/forms.py:526
_.FormDeleteControl  # class: plugin/writer/specialized/forms.py:895
_.FormEditControl  # class: plugin/writer/specialized/forms.py:812
_.FormGenerate  # class: plugin/writer/specialized/forms.py:597
_.FormListControls  # class: plugin/writer/specialized/forms.py:755
_.FrameGetInfo  # class: plugin/writer/specialized/textframes.py:96
_.FrameList  # class: plugin/writer/specialized/textframes.py:68
_.FrameSetProperties  # class: plugin/writer/specialized/textframes.py:138
_.GetDocumentContent  # class: plugin/writer/get_document_content.py:56
_.GetDocumentTree  # class: plugin/writer/outline.py:40
_.GetDrawTree  # class: plugin/draw/tree.py:468
_.GetGuidance  # class: plugin/doc/document_research_tools.py:109
_.GetHeadersFooters  # class: plugin/draw/headers_footers.py:302
_.GetImage  # class: plugin/writer/get_image.py:175
_.GetIndexStats  # class: plugin/writer/search.py:1213
_.GetPageObjects  # class: plugin/writer/structural.py:96
_.GetPlaceholderText  # class: plugin/draw/placeholders.py:190
_.GetPresentationInfo  # class: plugin/draw/pages.py:234
_.GetSheetFilter  # class: plugin/calc/sheet_filter.py:241
_.GetSheetSummary  # class: plugin/calc/sheets.py:187
_.GetSlideLayout  # class: plugin/draw/transitions.py:303
_.GetSlideMaster  # class: plugin/draw/masters.py:43
_.GetSlideTransition  # class: plugin/draw/transitions.py:179
_.GetSpeakerNotes  # class: plugin/draw/notes.py:26
_.GoalSeekTool  # class: plugin/calc/analysis.py:72
_.GrepNearbyFiles  # class: plugin/doc/document_research_grep_tool.py:18
_.ImageCropAndHighlight  # class: plugin/writer/images/images.py:1027
_.ImageDelete  # class: plugin/writer/images/images.py:1422
_.ImageDownload  # class: plugin/writer/images/images.py:1165
_.ImageGenerate  # class: plugin/writer/images/images.py:123
_.ImageGetInfo  # class: plugin/writer/images/images.py:648
_.ImageInsert  # class: plugin/writer/images/images.py:1223
_.ImageList  # class: plugin/writer/images/images.py:482
_.ImageListNearbyFiles  # class: plugin/writer/images/images.py:433
_.ImageReplace  # class: plugin/writer/images/images.py:1470
_.ImageSetProperties  # class: plugin/writer/images/images.py:819
_.IndexesAddMark  # class: plugin/writer/specialized/indexes.py:1529
_.IndexesCreate  # class: plugin/writer/specialized/indexes.py:1440
_.IndexesDeleteTocEntry  # class: plugin/writer/specialized/indexes.py:1020
_.IndexesInsertTocEntry  # class: plugin/writer/specialized/indexes.py:1090
_.IndexesList  # class: plugin/writer/specialized/indexes.py:1305
_.IndexesListCites  # class: plugin/writer/specialized/indexes.py:1386
_.IndexesListTocEntries  # class: plugin/writer/specialized/indexes.py:1335
_.IndexesRefreshTocEntry  # class: plugin/writer/specialized/indexes.py:881
_.IndexesUpdateAll  # class: plugin/writer/specialized/indexes.py:321
_.InsertCellHtml  # class: plugin/calc/cells.py:686
_.InsertField  # class: plugin/writer/specialized/mail_merge.py:234
_.InsertMathDraw  # class: plugin/draw/math_insert.py:148
_.LibrarianOnboardingTool  # class: plugin/chatbot/librarian.py:227
_.ListCalcFunctions  # class: plugin/calc/formulas.py:121
_.ListCellComments  # class: plugin/calc/comments.py:83
_.ListConditionalFormats  # class: plugin/calc/conditional.py:93
_.ListDataSources  # class: plugin/writer/specialized/mail_merge.py:85
_.ListDesigns  # class: plugin/draw/designs.py:1325
_.ListFields  # class: plugin/writer/specialized/mail_merge.py:342
_.ListMasterSlides  # class: plugin/draw/masters.py:19
_.ListNearbyFiles  # class: plugin/doc/document_research_tools.py:19
_.ListOpenDocuments  # class: plugin/doc/document_research_tools.py:69
_.ListPages  # class: plugin/draw/pages.py:162
_.ListPivotTables  # class: plugin/calc/pivot.py:242
_.ListPlaceholders  # class: plugin/draw/placeholders.py:174
_.ListSheets  # class: plugin/calc/sheets.py:42
_.ManageTableStructure  # class: plugin/writer/specialized/tables.py:691
_.ManageTrackedChanges  # class: plugin/writer/tracking.py:363
_.Maximize  # attribute: plugin/calc/analysis.py:249
_.MergeCells  # class: plugin/calc/cells.py:866
_.MoveSlide  # class: plugin/draw/pages.py:331
_.NamedRangeAdd  # class: plugin/calc/named_ranges.py:371
_.NamedRangeCreateFromTitles  # class: plugin/calc/named_ranges.py:529
_.NamedRangeDelete  # class: plugin/calc/named_ranges.py:500
_.NamedRangeEdit  # class: plugin/calc/named_ranges.py:417
_.NamedRangeGetInfo  # class: plugin/calc/named_ranges.py:336
_.NamedRangeList  # class: plugin/calc/named_ranges.py:297
_.NavGotoPage  # class: plugin/writer/structural.py:81
_.NavHeading  # class: plugin/writer/navigation.py:31
_.NavHeadingChildren  # class: plugin/writer/navigation.py:81
_.NavSurroundings  # class: plugin/writer/navigation.py:57
_.Objective  # attribute: plugin/calc/analysis.py:250
_.OptimizeDataTool  # class: plugin/calc/optimize.py:21
_.OutputType  # attribute: plugin/writer/specialized/mail_merge.py:538
_.OutputURL  # attribute: plugin/writer/specialized/mail_merge.py:546
_.PageGetColumns  # class: plugin/writer/page.py:1611
_.PageGetHeaderFooterText  # class: plugin/writer/page.py:930
_.PageGetStyleProperties  # class: plugin/writer/page.py:763
_.PageInsertBreak  # class: plugin/writer/page.py:1759
_.PageReplaceHeaderFooterText  # class: plugin/writer/page.py:1436
_.PageSetColumns  # class: plugin/writer/page.py:1644
_.PageSetHeaderFooterText  # class: plugin/writer/page.py:1026
_.PageSetStyleProperties  # class: plugin/writer/page.py:779
_.PlotDataTool  # class: plugin/calc/viz.py:22
_.ProtectSheet  # class: plugin/calc/sheets.py:156
_.QueryFolderSqlTool  # class: plugin/calc/duckdb_tools.py:37
_.ReadCellRange  # class: plugin/calc/cells.py:412
_.ReadSlideText  # class: plugin/draw/pages.py:189
_.Redo  # class: plugin/doc/undo.py:142
_.RefreshPivotTable  # class: plugin/calc/pivot.py:208
_.RegisterDataSource  # class: plugin/writer/specialized/mail_merge.py:167
_.RemoveConditionalFormats  # class: plugin/calc/conditional.py:214
_.RenameSheet  # class: plugin/calc/sheets.py:108
_.RenameSlide  # class: plugin/draw/pages.py:357
_.ReplaceInSpreadsheet  # class: plugin/calc/search.py:69
_.RunMerge  # class: plugin/writer/specialized/mail_merge.py:424
_.RunVenvPythonScript  # class: plugin/calc/python/venv.py:131
_.SaveAsSingleFile  # attribute: plugin/writer/specialized/mail_merge.py:547
_.SaveDesignSpec  # class: plugin/chatbot/brainstorming.py:84
_.SaveFilter  # attribute: plugin/writer/specialized/mail_merge.py:553
_.SearchInDocument  # class: plugin/writer/search.py:956
_.SearchInSpreadsheet  # class: plugin/calc/search.py:31
_.SearchNearbyFiles  # class: plugin/embeddings/document_research_fts_tool.py:21
_.SectionList  # class: plugin/writer/structural.py:63
_.SectionRead  # class: plugin/writer/structural.py:276
_.SendPeerResult  # class: plugin/doc/peer_message.py:812
_.SendPeerWork  # class: plugin/doc/peer_message.py:804
_.SetActivePage  # class: plugin/draw/pages.py:274
_.SetCellStyle  # class: plugin/calc/cells.py:718
_.SetHeadersFooters  # class: plugin/draw/headers_footers.py:337
_.SetPlaceholderText  # class: plugin/draw/placeholders.py:221
_.SetSelection  # class: plugin/writer/selection.py:34
_.SetSlideLayout  # class: plugin/draw/transitions.py:319
_.SetSlideMaster  # class: plugin/draw/masters.py:59
_.SetSlideTransition  # class: plugin/draw/transitions.py:213
_.SetSpeakerNotes  # class: plugin/draw/notes.py:43
_.SolverTool  # class: plugin/calc/analysis.py:131
_.SortRange  # class: plugin/calc/cells.py:904
_.SpecializedWorkflowFinished  # class: plugin/writer/specialized_base.py:293
_.StyleClone  # class: plugin/writer/styles.py:1423
_.StyleCreate  # class: plugin/writer/styles.py:1287
_.StyleGetInfo  # class: plugin/writer/styles.py:840
_.StyleImport  # class: plugin/writer/styles.py:1507
_.StyleList  # class: plugin/writer/styles.py:731
_.StyleUpdate  # class: plugin/writer/styles.py:1165
_.SwitchSheet  # class: plugin/calc/sheets.py:57
_.SymbolicMathTool  # class: plugin/calc/symbolic_math.py:26
_.TableDelete  # class: plugin/writer/specialized/tables.py:912
_.TableGetCells  # class: plugin/writer/specialized/tables.py:511
_.TableInsert  # class: plugin/writer/specialized/tables.py:773
_.TableList  # class: plugin/writer/specialized/tables.py:461
_.TableSetCell  # class: plugin/writer/specialized/tables.py:620
_.ToolCalcDocumentResearchBase  # class: plugin/calc/base.py:61
_.ToolCalcImageBase  # class: plugin/calc/base.py:45
_.ToolCalcWebResearchBase  # class: plugin/calc/base.py:56
_.ToolDrawDocumentResearchBase  # class: plugin/draw/base.py:52
_.ToolDrawPythonBase  # class: plugin/draw/base.py:105
_.ToolDrawWebResearchBase  # class: plugin/draw/base.py:47
_.ToolWriterDocumentResearchBase  # class: plugin/writer/specialized_base.py:282
_.ToolWriterPythonBase  # class: plugin/writer/specialized_base.py:191
_.ToolWriterVisionBase  # class: plugin/writer/specialized_base.py:177
_.ToolWriterWebResearchBase  # class: plugin/writer/specialized_base.py:277
_.TrackChangesList  # class: plugin/writer/tracking.py:256
_.TrackChangesShow  # class: plugin/writer/tracking.py:323
_.TrackChangesStart  # class: plugin/writer/tracking.py:230
_.TrackChangesStop  # class: plugin/writer/tracking.py:243
_.TransformDocumentStructure  # class: plugin/draw/transform.py:17
_.Undo  # class: plugin/doc/undo.py:124
_.ValidatePptMasterProject  # class: plugin/ppt_master/tools.py:58
_.Variables  # attribute: plugin/calc/analysis.py:251
_.WriteCellRange  # class: plugin/calc/cells.py:488
_.WriteDocumentSection  # class: plugin/chatbot/writing.py:61
_.WritingPlanSessionTool  # class: plugin/chatbot/writing.py:129
_.WritingResearchWeb  # class: plugin/chatbot/writing.py:51
_.long_running  # variable: plugin/calc/analysis.py:354

# =============================================================================
# 3. ServiceRegistry and Scripting Domain Registries
#
# Services registered with ServiceRegistry and trusted dispatch functions
# dispatched dynamically by string name across the IPC boundary. Vulture
# cannot trace dynamic string-based dispatch.
# =============================================================================
_.IndexService  # class: plugin/writer/locale/linguistic_index.py:145
_.ProximityService  # class: plugin/writer/proximity.py:62
_.dispatch_embedding  # function: plugin/scripting/venv/trusted_dispatch.py:160
_.dispatch_langdetect  # function: plugin/scripting/venv/trusted_dispatch.py:168
_.dispatch_languagetool  # function: plugin/scripting/venv/trusted_dispatch.py:141
_.dispatch_spec_domain  # function: plugin/scripting/venv/trusted_dispatch.py
_.dispatch_sql  # function: plugin/scripting/venv/trusted_dispatch.py:126
_.dispatch_text  # function: plugin/scripting/venv/trusted_dispatch.py:109
_.dispatch_vale  # function: plugin/scripting/venv/trusted_dispatch.py:150
_.dispatch_vision  # function: plugin/scripting/venv/trusted_dispatch.py:119
_.initialize_all  # method: plugin/framework/service.py:175
_.service_names  # other: plugin/framework/service.py:183

# =============================================================================
# 4. Test-Only Code, Fixtures, Hooks, and Samples
#
# Test hooks, reset helpers, mock injector functions, and runner utilities
# defined in plugin/ that are called exclusively from tests/ (which Vulture
# does not scan by default).
# =============================================================================
_.TTS_TEST_SAMPLE  # variable: plugin/audio/tts_voices.py:76
_.adopt_chat_sidebar  # function: plugin/chatbot/sidebar_test_hooks.py:1673
_.approval_active  # function: plugin/chatbot/sidebar_test_hooks.py:2613
_.assert_no_empty_graphics  # function: plugin/chatbot/sidebar_test_hooks.py:3106
_.audio_status  # function: plugin/chatbot/sidebar_test_hooks.py:2684
_.calc_addin_args_to_python  # function: plugin/calc/calc_addin_data.py:130
_.clear_all_document_persistence  # function: plugin/writer/locale/grammar_persistence.py:778
_.clear_all_sandbox_sessions  # function: plugin/scripting/venv/venv_sandbox.py:1116
_.clear_fake_stt  # function: plugin/chatbot/sidebar_test_hooks.py:611
_.clear_fake_tts  # function: plugin/chatbot/sidebar_test_hooks.py:926
_.clear_for_doc  # method: plugin/writer/locale/grammar_persistence.py:173
_.clear_page_shapes  # function: plugin/ppt_master/adapter/uno_shape_postprocess.py:225
_.clear_python_code_hot_cache  # function: plugin/scripting/sandbox_cache.py:230
_.clear_research_fluff_words_cache  # function: plugin/chatbot/web_research_cache.py:165
_.clear_session  # function: plugin/ppt_master/venv/runner.py:332
_.clear_sidebar_chat  # function: plugin/chatbot/sidebar_test_hooks.py:2499
_.close_calc_document  # function: plugin/chatbot/sidebar_test_hooks.py:1579
_.dispatch_undo  # function: plugin/chatbot/sidebar_test_hooks.py:3116
_.ensure_sidebar_chat_mode  # function: plugin/chatbot/sidebar_test_hooks.py:2079
_.flush_sheet_modify_pass_for_tests  # function: plugin/calc/python/sheet_modify.py:207
_.get_drain_depth  # function: plugin/framework/async_drain_guard.py:147
_.get_draw_graphics_count  # function: plugin/chatbot/sidebar_test_hooks.py:3094
_.get_suppressed_vcl_pump_count  # function: plugin/framework/async_drain_guard.py:173
_.get_thread_guard_violations  # function: plugin/chatbot/sidebar_test_hooks.py:2873
_.get_writer_graphics_count  # function: plugin/chatbot/sidebar_test_hooks.py:3087
_.inflate_sidebar_history  # function: plugin/chatbot/sidebar_test_hooks.py:2480
_.inject_wav  # function: plugin/chatbot/sidebar_test_hooks.py:2717
_.insert_user_redline  # function: plugin/chatbot/sidebar_test_hooks.py:2934
_.install_fake_stt  # function: plugin/chatbot/sidebar_test_hooks.py:581
_.install_fake_tts  # function: plugin/chatbot/sidebar_test_hooks.py:915
_.list_redlines  # function: plugin/chatbot/sidebar_test_hooks.py:2895
_.listener_queue_len  # function: plugin/doc/peer_message.py:327
_.native_test  # function: plugin/testing_runner.py:1381
_.next_hello_ok  # function: plugin/chatbot/sidebar_test_hooks.py:2960
_.open_calc_document  # function: plugin/chatbot/sidebar_test_hooks.py:1648
_.press_accept  # function: plugin/chatbot/sidebar_test_hooks.py:2585
_.press_change  # function: plugin/chatbot/sidebar_test_hooks.py:2593
_.press_query_key  # function: plugin/chatbot/sidebar_test_hooks.py:2439
_.press_record  # function: plugin/chatbot/sidebar_test_hooks.py:2621
_.press_reject  # function: plugin/chatbot/sidebar_test_hooks.py:2604
_.press_send_clicked  # function: plugin/chatbot/sidebar_test_hooks.py:2661
_.press_stop  # function: plugin/chatbot/sidebar_test_hooks.py:2561
_.press_stop_mouse  # function: plugin/chatbot/sidebar_test_hooks.py:2578
_.press_stop_rec  # function: plugin/chatbot/sidebar_test_hooks.py:2639
_.request_office_recycle_after_suite  # function: plugin/testing_runner.py:1133
_.reset_config_for_tests  # function: plugin/framework/config.py:186
_.reset_desktop_create_is_unsafe_for_tests  # function: plugin/framework/uno_context.py:112
_.reset_extension_update_check_schedule_for_tests  # function: plugin/chatbot/extension_update_check.py:138
_.reset_frame_sessions  # function: plugin/framework/frame_session.py:119
_.reset_geometric_runtime_for_tests  # function: plugin/calc/python/geometric_recalc.py:79
_.reset_grammar_llm_client_cache  # function: plugin/writer/locale/grammar_worker.py:47
_.reset_host_pacing_for_tests  # function: plugin/framework/client/request_controls.py:236
_.reset_i18n  # function: plugin/framework/i18n.py:52
_.reset_live_panels  # function: plugin/doc/live_panels.py:61
_.reset_local_unverified_hosts_for_tests  # function: plugin/framework/client/request_controls.py:399
_.reset_menu_graphic_cache  # function: plugin/librepy/sidebar_menus.py:49
_.reset_native_cell_editor_for_tests  # function: plugin/calc/python/cell_editor_ui.py:308
_.reset_package_extension_id_for_tests  # function: plugin/framework/uno_context.py:190
_.reset_peer_queues  # function: plugin/doc/peer_message.py:384
_.reset_sentry_state  # function: plugin/framework/async_drain_guard.py:241
_.reset_suppressed_vcl_pump_count  # function: plugin/framework/async_drain_guard.py:179
_.set_drain_scheduler_override  # function: plugin/framework/async_stream.py:1145
_.set_fake_vision  # function: plugin/chatbot/sidebar_test_hooks.py:3075
_.set_late_transcript_delay  # function: plugin/chatbot/sidebar_test_hooks.py:331
_.set_test_poke_handler  # function: plugin/framework/queue_executor.py:72
_.show_window  # variable: plugin/testing_runner.py:647
_.sidebar_deck_names  # function: plugin/chatbot/sidebar_test_hooks.py:1719
_.slash_popup_state  # function: plugin/chatbot/sidebar_test_hooks.py:2412
_.stop_label  # variable: plugin/chatbot/sidebar_test_hooks.py:2787
_.stub_recorder_child  # function: plugin/chatbot/sidebar_test_hooks.py:2738
_.teardown  # function: plugin/testing_runner.py:1397
_.throwaway_writeragent_json  # function: plugin/testing_runner.py:733
_.toggle_record_changes  # function: plugin/chatbot/sidebar_test_hooks.py:2930
_.transcript_contains  # function: plugin/chatbot/sidebar_test_hooks.py:2475
_.wait_controls_send_finished  # function: plugin/chatbot/sidebar_test_hooks.py:2123
