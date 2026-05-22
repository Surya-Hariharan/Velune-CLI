"""Python symbol extractor."""

from typing import list, Dict, Any
from velune.core.types import SymbolNode


class PythonSymbolExtractor:
    """Extracts symbols from Python code."""

    def extract(self, ast_tree, file_path: str) -> list[SymbolNode]:
        """Extract symbols from Python AST."""
        symbols = []
        
        def traverse(node, parent=None):
            if node is None:
                return
            
            node_type = node.type
            
            if node_type == "function_definition":
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
                    traverse(node, name)
            
            elif node_type == "class_definition":
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
                    traverse(node, name)
            
            else:
                for child in node.children:
                    traverse(child, parent)
        
        traverse(ast_tree.root_node)
        return symbols
