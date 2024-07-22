from typing import Iterable, Sequence

from langchain_community.graph_vectorstores.extractors import LinkExtractor
from langchain_core.documents import Document
from langchain_core.documents.transformers import BaseDocumentTransformer
from langchain_core.graph_vectorstores.links import add_links


class LinkExtractorTransformer(BaseDocumentTransformer):
    def __init__(self, link_extractors: Iterable[LinkExtractor[Document]]):
        """Create a DocumentTransformer which adds the given links."""
        self.link_extractors = link_extractors

    def transform_documents(self, documents: Sequence[Document]) -> Sequence[Document]:
        document_links = zip(
            documents,
            zip(
                *[
                    extractor.extract_many(documents)
                    for extractor in self.link_extractors
                ]
            ),
        )
        for document, links in document_links:
            add_links(document, *links)
        return documents
