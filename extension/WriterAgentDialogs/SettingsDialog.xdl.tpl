<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE dlg:window PUBLIC "-//OpenOffice.org//DTD OfficeDocument 1.0//EN" "dialog.dtd">
<dlg:window xmlns:dlg="http://openoffice.org/2000/dialog" xmlns:script="http://openoffice.org/2000/script"
  dlg:id="SettingsDialog" dlg:left="100" dlg:top="50" dlg:width="440" dlg:height="200"
  dlg:closeable="true" dlg:moveable="true" dlg:resizeable="false" dlg:title="Settings"
  dlg:page="1">
 <dlg:bulletinboard>

  <!-- Tab buttons (always visible, no page attribute) -->
  <!-- Tab buttons (always visible, no page attribute) -->
  <dlg:button dlg:id="btn_tab_chat" dlg:left="5" dlg:top="5" dlg:width="60" dlg:height="14" dlg:value="Chat / Text"/>
  <dlg:button dlg:id="btn_tab_image" dlg:left="68" dlg:top="5" dlg:width="60" dlg:height="14" dlg:value="Image Settings"/>

  <!-- AUTO_GENERATED_TABS -->

  <!-- === Page 1: Chat / Text Settings === -->
  <dlg:text dlg:id="label_endpoint" dlg:page="1" dlg:left="8" dlg:top="26" dlg:width="150" dlg:height="10" dlg:value="Endpoint URL (/v1 added):" dlg:align="left"/>
  <dlg:combobox dlg:id="endpoint" dlg:page="1" dlg:left="165" dlg:top="24" dlg:width="265" dlg:height="14" dlg:tabstop="true" dlg:spin="true" dlg:dropdown="true" dlg:value="" dlg:border="1"/>

  <dlg:text dlg:id="label_api_key" dlg:page="1" dlg:left="8" dlg:top="42" dlg:width="150" dlg:height="10" dlg:value="API Key:" dlg:align="left"/>
  <dlg:textfield dlg:id="api_key" dlg:page="1" dlg:left="165" dlg:top="40" dlg:width="265" dlg:height="14" dlg:tabstop="true" dlg:value=""/>

  <dlg:text dlg:id="label_text_model" dlg:page="1" dlg:left="8" dlg:top="58" dlg:width="150" dlg:height="10" dlg:value="Text/Chat Model:" dlg:align="left"/>
  <dlg:combobox dlg:id="text_model" dlg:page="1" dlg:left="165" dlg:top="56" dlg:width="265" dlg:height="14" dlg:tabstop="true" dlg:spin="true" dlg:dropdown="true" dlg:value="" dlg:border="1"/>

  <dlg:text dlg:id="label_image_model" dlg:page="1" dlg:left="8" dlg:top="74" dlg:width="150" dlg:height="10" dlg:value="Image Model:" dlg:align="left"/>
  <dlg:combobox dlg:id="image_model" dlg:page="1" dlg:left="165" dlg:top="72" dlg:width="265" dlg:height="14" dlg:tabstop="true" dlg:spin="true" dlg:dropdown="true" dlg:value="" dlg:border="1"/>

  <dlg:text dlg:id="label_stt_model" dlg:page="1" dlg:left="8" dlg:top="90" dlg:width="150" dlg:height="10" dlg:value="Audio Model:" dlg:align="left"/>
  <dlg:combobox dlg:id="stt_model" dlg:page="1" dlg:left="165" dlg:top="88" dlg:width="265" dlg:height="14" dlg:tabstop="true" dlg:spin="true" dlg:dropdown="true" dlg:value="" dlg:border="1"/>

  <!-- Row 6: Temperature -->
  <dlg:text dlg:id="label_temperature" dlg:page="1" dlg:left="8" dlg:top="106" dlg:width="58" dlg:height="10" dlg:value="Temperature:" dlg:align="left"/>
  <dlg:textfield dlg:id="temperature" dlg:page="1" dlg:left="68" dlg:top="104" dlg:width="50" dlg:height="14" dlg:tabstop="true" dlg:value="-1"/>

  <!-- Row 7: Max Tokens + Context Len -->
  <dlg:text dlg:id="label_chat_max_tokens" dlg:page="1" dlg:left="8" dlg:top="122" dlg:width="58" dlg:height="10" dlg:value="Max Tokens:" dlg:align="left"/>
  <dlg:textfield dlg:id="chat_max_tokens" dlg:page="1" dlg:left="68" dlg:top="120" dlg:width="60" dlg:height="14" dlg:tabstop="true" dlg:value="16384"/>
  <dlg:text dlg:id="label_chat_context_length" dlg:page="1" dlg:left="140" dlg:top="122" dlg:width="58" dlg:height="10" dlg:value="Context Len:" dlg:align="left"/>
  <dlg:textfield dlg:id="chat_context_length" dlg:page="1" dlg:left="200" dlg:top="120" dlg:width="60" dlg:height="14" dlg:tabstop="true" dlg:value="8000"/>

  <!-- Row 8: Additional Instructions -->
  <dlg:text dlg:id="label_additional_instructions" dlg:page="1" dlg:left="8" dlg:top="138" dlg:width="150" dlg:height="10" dlg:value="Additional Instructions:" dlg:align="left"/>
  <dlg:combobox dlg:id="additional_instructions" dlg:page="1" dlg:left="165" dlg:top="136" dlg:width="265" dlg:height="14" dlg:tabstop="true" dlg:spin="true" dlg:dropdown="true" dlg:value="" dlg:border="1"/>

  <!-- Search Settings -->
  <dlg:checkbox dlg:id="show_search_thinking" dlg:page="1" dlg:left="8" dlg:top="154" dlg:width="150" dlg:height="10" dlg:value="Show Web Search Thinking" dlg:checked="false"/>



  <!-- === Page 2: Image Settings === -->
  <!-- Section 1: shared (all providers). Section 2: AI Horde only (below fixedline). -->

  <!-- Section 1: Image settings (all providers) -->
  <dlg:text dlg:id="label_image_base_size" dlg:page="2" dlg:left="8" dlg:top="26" dlg:width="60" dlg:height="10" dlg:value="Base Size:" dlg:align="left"/>
  <dlg:combobox dlg:id="image_base_size" dlg:page="2" dlg:left="70" dlg:top="24" dlg:width="50" dlg:height="14" dlg:tabstop="true" dlg:spin="true" dlg:dropdown="true" dlg:value="512" dlg:border="1"/>
  <dlg:text dlg:id="label_image_default_aspect" dlg:page="2" dlg:left="130" dlg:top="26" dlg:width="70" dlg:height="10" dlg:value="Aspect Ratio:" dlg:align="left"/>
  <dlg:combobox dlg:id="image_default_aspect" dlg:page="2" dlg:left="205" dlg:top="24" dlg:width="120" dlg:height="14" dlg:tabstop="true" dlg:spin="true" dlg:dropdown="true" dlg:value="Square" dlg:border="1"/>

  <dlg:checkbox dlg:id="image_auto_gallery" dlg:page="2" dlg:left="8" dlg:top="44" dlg:width="100" dlg:height="10" dlg:value="Auto Gallery" dlg:checked="true"/>
  <dlg:checkbox dlg:id="image_insert_frame" dlg:page="2" dlg:left="120" dlg:top="44" dlg:width="100" dlg:height="10" dlg:value="Insert Frame" dlg:checked="false"/>

  <dlg:checkbox dlg:id="image_translate_prompt" dlg:page="2" dlg:left="8" dlg:top="60" dlg:width="130" dlg:height="10" dlg:value="Translate Prompt" dlg:checked="true"/>

  <dlg:text dlg:id="label_image_translate_from" dlg:page="2" dlg:left="8" dlg:top="76" dlg:width="150" dlg:height="10" dlg:value="Translate from (e.g. es):" dlg:align="left"/>
  <dlg:textfield dlg:id="image_translate_from" dlg:page="2" dlg:left="165" dlg:top="74" dlg:width="265" dlg:height="14" dlg:tabstop="true" dlg:value=""/>

  <dlg:text dlg:id="label_seed" dlg:page="2" dlg:left="8" dlg:top="92" dlg:width="150" dlg:height="10" dlg:value="Seed (for reproducibility):" dlg:align="left"/>
  <dlg:textfield dlg:id="seed" dlg:page="2" dlg:left="165" dlg:top="90" dlg:width="120" dlg:height="14" dlg:tabstop="true" dlg:value=""/>

  <!-- Visual separator line (no label so line is visible), then label and section below -->
  <dlg:fixedline dlg:id="fixedline_horde" dlg:page="2" dlg:left="8" dlg:top="108" dlg:width="424" dlg:height="6" dlg:value=""/>
  <dlg:checkbox dlg:id="use_aihorde" dlg:page="2" dlg:left="8" dlg:top="116" dlg:width="250" dlg:height="10" dlg:value="Use AI Horde for Image Generation" dlg:checked="false"/>

  <!-- Section 2: AI Horde only -->
  <dlg:text dlg:id="label_aihorde_api_key" dlg:page="2" dlg:left="8" dlg:top="132" dlg:width="150" dlg:height="10" dlg:value="AI Horde API Key:" dlg:align="left"/>
  <dlg:textfield dlg:id="aihorde_api_key" dlg:page="2" dlg:left="165" dlg:top="130" dlg:width="265" dlg:height="14" dlg:tabstop="true" dlg:value=""/>

  <dlg:text dlg:id="label_image_cfg_scale" dlg:page="2" dlg:left="8" dlg:top="148" dlg:width="60" dlg:height="10" dlg:value="CFG Scale:" dlg:align="left"/>
  <dlg:textfield dlg:id="image_cfg_scale" dlg:page="2" dlg:left="70" dlg:top="146" dlg:width="50" dlg:height="14" dlg:tabstop="true" dlg:value="7.5"/>
  <dlg:text dlg:id="label_image_steps" dlg:page="2" dlg:left="130" dlg:top="148" dlg:width="50" dlg:height="10" dlg:value="Steps:" dlg:align="left"/>
  <dlg:textfield dlg:id="image_steps" dlg:page="2" dlg:left="182" dlg:top="146" dlg:width="50" dlg:height="14" dlg:tabstop="true" dlg:value="30"/>
  <dlg:text dlg:id="label_image_max_wait" dlg:page="2" dlg:left="242" dlg:top="148" dlg:width="60" dlg:height="10" dlg:value="Max Wait (s):" dlg:align="left"/>
  <dlg:textfield dlg:id="image_max_wait" dlg:page="2" dlg:left="304" dlg:top="146" dlg:width="40" dlg:height="14" dlg:tabstop="true" dlg:value="5"/>

  <dlg:checkbox dlg:id="image_nsfw" dlg:page="2" dlg:left="8" dlg:top="166" dlg:width="100" dlg:height="10" dlg:value="Allow NSFW" dlg:checked="false"/>
  <dlg:checkbox dlg:id="image_censor_nsfw" dlg:page="2" dlg:left="120" dlg:top="166" dlg:width="100" dlg:height="10" dlg:value="Censor NSFW" dlg:checked="true"/>

  <!-- AUTO_GENERATED_PAGES -->


  <!-- OK Button (always visible, at bottom of dialog with small margin) -->
  <dlg:button dlg:id="btn_ok" dlg:left="170" dlg:top="176" dlg:width="100" dlg:height="18" dlg:value="OK" dlg:button-type="ok" dlg:default="true"/>
 </dlg:bulletinboard>
</dlg:window>
