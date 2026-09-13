import streamlit as st
from streamlit import session_state as ss
from my_knowledge_source import MyKnowledgeSource
import db_utils
import os
import shutil
from pathlib import Path
from i18n import t

class PageKnowledge:
    def __init__(self):
        self.name = t("page.knowledge")

    def create_knowledge_source(self):
        knowledge_source = MyKnowledgeSource()
        if 'knowledge_sources' not in ss:
            ss.knowledge_sources = []
        ss.knowledge_sources.append(knowledge_source)
        knowledge_source.edit = True
        db_utils.save_knowledge_source(knowledge_source)
        return knowledge_source

    def clear_knowledge(self):
        # This will clear knowledge stores in CrewAI
        knowledge_dir = Path(os.environ.get("CREWAI_KNOWLEDGE_DIR", "/var/lib/crewai/knowledge"))
        if knowledge_dir.exists():
            shutil.rmtree(knowledge_dir)
            st.success("Knowledge stores cleared successfully!")
        else:
            st.info("No knowledge stores found to clear.")

    def draw(self):
        st.subheader(self.name)

        # Instruction
        st.markdown(t("knowledge.description"))

        # Create knowledge directory if it doesn't exist
        os.makedirs(os.environ.get("CREWAI_KNOWLEDGE_DIR", "/var/lib/crewai/knowledge"), exist_ok=True)

        # Clear knowledge button
        st.button(t("button.clear_knowledge"), on_click=self.clear_knowledge,
                  help=t("knowledge.clear_help"))
        
        # Display existing knowledge sources
        editing = False
        if 'knowledge_sources' not in ss:
            ss.knowledge_sources = db_utils.load_knowledge_sources()
            
        for knowledge_source in ss.knowledge_sources:
            knowledge_source.draw()
            if knowledge_source.edit:
                editing = True
                
        if len(ss.knowledge_sources) == 0:
            st.write(t("knowledge.none_defined"))

        st.button(t("button.create_knowledge"), on_click=self.create_knowledge_source, disabled=editing)
