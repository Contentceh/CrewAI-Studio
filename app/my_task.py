from crewai import Task
import streamlit as st
from utils import rnd_id, fix_columns_width
from streamlit import session_state as ss
from db_utils import save_task, delete_task
from datetime import datetime
from i18n import t

class MyTask:
    def __init__(self, id=None, description=None, expected_output=None, agent=None, async_execution=None, created_at=None, context_from_async_tasks_ids=None, context_from_sync_tasks_ids=None, **kwargs):
        self.id = id or "T_" + rnd_id()
        self.description = description or "Identify the next big trend in AI. Focus on identifying pros and cons and the overall narrative."
        self.expected_output = expected_output or "A comprehensive 3 paragraphs long report on the latest AI trends."
        self.agent = agent or ss.agents[0] if ss.agents else None
        self.async_execution = async_execution or False
        self.context_from_async_tasks_ids = context_from_async_tasks_ids or None
        self.context_from_sync_tasks_ids = context_from_sync_tasks_ids or None
        self.created_at = created_at or datetime.now().isoformat()
        self.edit_key = f'edit_{self.id}'
        if self.edit_key not in ss:
            ss[self.edit_key] = False

    @property
    def edit(self):
        return ss[self.edit_key]

    @edit.setter
    def edit(self, value):
        ss[self.edit_key] = value

    def get_crewai_task(self, context_from_async_tasks=None, context_from_sync_tasks=None, crewai_agent=None) -> Task:
        context = []
        if context_from_async_tasks:
            context.extend(context_from_async_tasks)
        if context_from_sync_tasks:
            context.extend(context_from_sync_tasks)
        
        if context:
            return Task(description=self.description, expected_output=self.expected_output, async_execution=self.async_execution, agent=crewai_agent or self.agent.get_crewai_agent(), context=context)
        else:
            return Task(description=self.description, expected_output=self.expected_output, async_execution=self.async_execution, agent=crewai_agent or self.agent.get_crewai_agent())

    def delete(self):
        ss.tasks = [task for task in ss.tasks if task.id != self.id]
        delete_task(self.id)

    def is_valid(self, show_warning=False):
        if not self.agent:
            if show_warning:
                st.warning(f"Task {self.description} has no agent")
            return False
        if not self.agent.is_valid(show_warning):
            return False
        return True

    def draw(self, key=None):
        agent_options = [agent.role for agent in ss.agents]
        expander_title = f"({self.agent.role if self.agent else 'unassigned'}) - {self.description}" if self.is_valid() else f"❗ ({self.agent.role if self.agent else 'unassigned'}) - {self.description}"
        if self.edit:
            with st.expander(expander_title, expanded=True):
                with st.form(key=f'form_{self.id}' if key is None else key):
                    self.description = st.text_area(t("task.description"), value=self.description)
                    self.expected_output = st.text_area(t("task.expected_output"), value=self.expected_output)
                    self.agent = st.selectbox(t("task.agent"), options=ss.agents, format_func=lambda x: x.role, index=0 if self.agent is None else agent_options.index(self.agent.role))
                    self.async_execution = st.checkbox(t("task.async_execution"), value=self.async_execution)
                    self.context_from_async_tasks_ids = st.multiselect(t("task.context_async"), options=[task.id for task in ss.tasks if task.async_execution], default=self.context_from_async_tasks_ids, format_func=lambda x: [task.description[:120] for task in ss.tasks if task.id == x][0])
                    self.context_from_sync_tasks_ids = st.multiselect(t("task.context_sync"), options=[task.id for task in ss.tasks if not task.async_execution], default=self.context_from_sync_tasks_ids, format_func=lambda x: [task.description[:120] for task in ss.tasks if task.id == x][0])
                    submitted = st.form_submit_button(t("button.save"))
                    if submitted:
                        self.set_editable(False)
        else:
            fix_columns_width()
            with st.expander(expander_title):
                st.markdown(f"**{t('task.description')}:** {self.description}")
                st.markdown(f"**{t('task.expected_output')}:** {self.expected_output}")
                st.markdown(f"**{t('task.agent')}:** {self.agent.role if self.agent else 'None'}")
                st.markdown(f"**{t('task.async_execution')}:** {self.async_execution}")
                st.markdown(f"**{t('task.context_async')}:** {', '.join([task.description[:120] for task in ss.tasks if task.id in self.context_from_async_tasks_ids]) if self.context_from_async_tasks_ids else 'None'}")
                st.markdown(f"**{t('task.context_sync')}:** {', '.join([task.description[:120] for task in ss.tasks if task.id in self.context_from_sync_tasks_ids]) if self.context_from_sync_tasks_ids else 'None'}")
                col1, col2 = st.columns(2)
                with col1:
                    st.button(t("button.edit"), on_click=self.set_editable, args=(True,), key=rnd_id())
                with col2:
                    st.button(t("button.delete"), on_click=self.delete, key=rnd_id())
                self.is_valid(show_warning=True)

    def set_editable(self, edit):
        self.edit = edit
        save_task(self)
        if not edit:
            st.rerun()
