"""MCP inventory and discovery health UI, separate from upstream pages."""

from datetime import datetime, timezone

import streamlit as st
from streamlit import session_state as ss

from mcp_health import CHECKS, status
from mcp_inventory import load_inventory, tool_rows


class PageMCP:
    def draw(self):
        st.subheader("MCP")
        st.caption("Подключения этой панели. Проверка выполняет initialize и tools/list, "
                   "не вызывает инструменты и не запускает LLM. Статус действителен 5 минут.")
        self.draw_connections()

    @staticmethod
    def check_all(connections):
        for connection in connections:
            CHECKS.submit(connection)

    @st.fragment(run_every="1s")
    def draw_connections(self):
        connections, warning = load_inventory()
        if warning:
            st.warning(warning)
        if not connections:
            st.info("MCP-подключения не настроены.")
            return
        st.button("Проверить все", key="mcp_check_all",
                  disabled=not any(c.enabled for c in connections),
                  on_click=self.check_all, args=(connections,))

        for connection in connections:
            result, pending = CHECKS.snapshot(connection)
            current = status(connection, result, pending)
            with st.container(border=True):
                st.subheader(connection.name)
                left, right = st.columns([3, 1])
                with left:
                    st.write(current)
                    st.text(f"Тип: {connection.transport}")
                    st.text("Подключение включено: " + ("Да" if connection.enabled else "Нет"))
                with right:
                    st.button("Проверить", key=f"mcp_check_{connection.adapter_id}",
                              disabled=not connection.enabled or pending,
                              on_click=CHECKS.submit, args=(connection,))
                if pending:
                    st.status("Ожидание результата MCP-проверки…", state="running")
                if not connection.registered:
                    st.info("Заготовка в политике: исполняемый MCP-профиль ещё не настроен. "
                            "Инструменты этого сервиса пока недоступны агентам.")
                checked = (datetime.fromtimestamp(result.checked_at, timezone.utc).strftime("%d.%m.%Y %H:%M:%S UTC")
                           if result else "Ещё не выполнялась")
                st.caption(f"Последняя проверка: {checked}")
                if result and current == "🔴 Не работает":
                    st.error(result.error)
                if current == "🟢 Работает":
                    st.caption("MCP-сервер ответил. Выполнение отдельных инструментов не проверялось.")
                rows = tool_rows(connection, ss.get("tools", []), ss.get("agents", []),
                                 result.tools if result and result.ok and result.fingerprint == connection.fingerprint else None)
                if rows:
                    st.dataframe(rows, hide_index=True, width="stretch")
                    st.caption("Ответ сервера относится к последней успешной проверке; разрешение адаптера, добавление в Tools "
                               "и назначение агенту — отдельные состояния. Назначение: Agents → Tools.")
                else:
                    st.caption("Сервер вернул пустой список инструментов." if result and result.ok else "Список инструментов ещё не получен.")
