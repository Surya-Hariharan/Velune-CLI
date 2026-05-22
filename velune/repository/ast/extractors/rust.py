"""Rust symbol extractor."""

from typing import list
from velune.core.types import SymbolNode


class RustSymbolExtractor:
    """Extracts symbols from Rust code."""

    def extract(self, ast_tree, file_path: str) -> list[SymbolNode]:
        """Extract symbols from Rust AST."""
        symbols = []
        
        def traverse(node, parent=None):
            if node is None:
                return
            
            node_type = node.type
            
            if node_type == "function_item":
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
            
            elif node_type in ["struct_item", "enum_item", "impl_item"]:
                name_node = node.child_by_field_name("name")
                if name_node:
                    name = name_node.text.decode("utf8")
                    symbol = SymbolNode(
                        name=name,
                        kind=node_type.replace("_item", ""),
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
