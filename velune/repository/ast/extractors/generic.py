"""Generic symbol extractor."""

from typing import list
from velune.core.types import SymbolNode


class GenericSymbolExtractor:
    """Generic symbol extractor for unsupported languages."""

    def extract(self, ast_tree, file_path: str) -> list[SymbolNode]:
        """Extract symbols using generic heuristics."""
        symbols = []
        
        def traverse(node, parent=None):
            if node is None:
                return
            
            # Try to identify function-like nodes
            if "function" in node.type.lower() or "def" in node.type.lower():
                name_node = node.child_by_field_name("name")
                if name_node:
                    name = name_node.text.decode("utf8")
                    symbol = SymbolNode(
                        name=name,
                        kind="function",
                        file_path=file_path,
                        line_start=node.start_point[0] + 1,
                        line_end=node.end_point[0] + 1,
                        parent=parent,
                    )
                    symbols.append(symbol)
            
            # Try to identify class-like nodes
            elif "class" in node.type.lower() or "struct" in node.type.lower():
                name_node = node.child_by_field_name("name")
                if name_node:
                    name = name_node.text.decode("utf8")
                    symbol = SymbolNode(
                        name=name,
                        kind="class",
                        file_path=file_path,
                        line_start=node.start_point[0] + 1,
                        line_end=node.end_point[0] + 1,
                        parent=parent,
                    )
                    symbols.append(symbol)
            
            for child in node.children:
                traverse(child, parent)
        
        traverse(ast_tree.root_node)
        return symbols
