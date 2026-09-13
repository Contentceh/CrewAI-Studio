import streamlit as st
from utils import rnd_id
from my_tools import TOOL_CLASSES
from streamlit import session_state as ss
import db_utils
from i18n import t
from mcp_tools import mcp_tool_metadata
from mcp_inventory import tool_origin

class PageTools:
    def __init__(self):
        self.name = t("page.tools")
        self.available_tools = TOOL_CLASSES

    def create_tool(self, tool_name):
        tool_class = self.available_tools[tool_name]
        tool_instance = tool_class(rnd_id())
        if 'tools' not in ss:
            ss.tools = []
        ss.tools.append(tool_instance)
        db_utils.save_tool(tool_instance)  # Save tool to database

    def remove_tool(self, tool_id):
        ss.tools = [tool for tool in ss.tools if tool.tool_id != tool_id]
        db_utils.delete_tool(tool_id)
        st.rerun()

    def set_tool_parameter(self, tool_id, param_name, value):
        if value == "":
            value = None
        for tool in ss.tools:
            if tool.tool_id == tool_id:
                tool.set_parameters(**{param_name: value})
                db_utils.save_tool(tool)
                break

    def get_tool_display_name(self, tool):
        first_param_name = tool.get_parameter_names()[0] if tool.get_parameter_names() else None
        first_param_value = tool.parameters.get(first_param_name, '') if first_param_name else ''
        return f"{tool.display_name} ({first_param_value if first_param_value else tool.tool_id})"

    def draw_tools(self):
        c1,c2 = st.columns([1, 3])
        #st.write("Available Tools:")
        with c1:
            for tool_name in self.available_tools.keys():
                tool_class = self.available_tools[tool_name]
                tool_instance = tool_class()
                tool_description = tool_instance.description
                if st.button(f"{tool_instance.display_name}", key=f"enable_{tool_name}", help=tool_description):
                    self.create_tool(tool_name)
        with c2:
            if 'tools' in ss:
                st.write(f"##### {t('tools.enabled_tools')}")
                for tool in ss.tools:
                    display_name = self.get_tool_display_name(tool)
                    is_complete = tool.is_valid()
                    expander_title = display_name if is_complete else f"❗ {display_name}"
                    with st.expander(expander_title):
                        st.write(tool.description)
                        origin = tool_origin(tool)
                        if origin:
                            source = "MCP Fixture — тестовый сервер" if origin[0] == "project_fixture" else origin[0]
                            st.text(f"MCP-сервер: {source} | Инструмент: {origin[1]}")
                            st.caption("Доступность подключения: вкладка MCP. Назначение агентам: Agents → Tools.")
                        if tool.name == "MCPFixtureTool":
                            st.caption(f"Adapter: {mcp_tool_metadata()['adapter_id']} | Tool: {mcp_tool_metadata()['remote_tool_name']} | Resources: {', '.join(mcp_tool_metadata()['approved_resources'])} | Timeout: {mcp_tool_metadata()['timeout_seconds']}s | Retries: {mcp_tool_metadata()['max_retries']}")
                        for param_name in tool.get_parameter_names():
                            param_value = tool.parameters.get(param_name, "")
                            placeholder = t("tools.required") if tool.is_parameter_mandatory(param_name) else t("tools.optional")
                            new_value = st.text_input(f"{param_name}", value=param_value, key=f"{tool.tool_id}_{param_name}", placeholder=placeholder)
                            if new_value != param_value:
                                self.set_tool_parameter(tool.tool_id, param_name, new_value)
                        if st.button(f"Remove", key=f"remove_{tool.tool_id}"):
                            self.remove_tool(tool.tool_id)

    def draw(self):
        st.subheader(self.name)
        self.draw_tools()
