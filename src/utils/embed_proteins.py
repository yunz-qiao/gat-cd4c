"""
embed_proteins.py  — revised
---------------------------
Build per‑protein *graphs* instead of fixed 1 283‑D ESM embeddings.

Each residue becomes a node with:
    • 20‑D one‑hot amino‑acid vector
    • 1‑D integer charge at pH ≈ 7.0  {‑1, 0, +1}
    • 3‑D Cα coordinates (x,y,z)
=> node feature dim = 24.

Edges connect residues that are either:
    • sequential neighbours (peptide bond)  OR
    • spatial neighbours within a distance < cut‑off (default 10 Å)
Edge feature = Euclidean distance [Å].

Graphs are saved in ``protein_graphs/<CHEMBL_ID>.pt``
and consumed later by ``utils.dataset.DrugProteinDataset``.

The loader still supports the old “ESM + coords” pathway:  
pass an ESM tensor to *build* and it will concatenate instead of one‑hotting.
"""

from __future__ import annotations

import os
import pathlib
from typing import Dict, Optional

import torch
from Bio.PDB import PDBParser
from torch_geometric.data import Data

__all__ = [
    "AA_TO_IDX",
    "AA_CHARGE",
    "NUM_AA",
    "ProteinGraphBuilder",
]

# ---------------------------------------------------------------------------
# Amino‑acid lookup tables
# ---------------------------------------------------------------------------
AA_ORDER = "ACDEFGHIKLMNPQRSTVWY"
AA_TO_IDX: Dict[str, int] = {aa: i for i, aa in enumerate(AA_ORDER)}
NUM_AA = len(AA_ORDER)                                                  # 20

# integer charge at pH≈7 (simplified)
AA_CHARGE: Dict[str, int] = {
    "D": -1,
    "E": -1,
    "K": +1,
    "R": +1,
    "H": +1,  # histidine is ~ +0.1 but bin to +1
}

# ---------------------------------------------------------------------------
class ProteinGraphBuilder:
    """Build *either* whole‑protein graphs (旧 behaviour) *or* pocket‑only graphs*.

    New args
    -----
    pocket_radius : float | None
        If given, residues whose C‑α atom is farther than this radius (Å) from **any** ligand
        heavy‑atom will be *discarded*, giving a smaller, task‑focused graph.
    """

    def __init__(self,
                 graph_dir: str = "../../data/protein_graphs",
                 cutoff: float = 10.0,
                 pocket_radius: float | None = None):
        self.cutoff = cutoff              # edge build distance threshold (Å)
        self.pocket_radius = pocket_radius
        self.graph_dir = pathlib.Path(graph_dir)
        self.parser = PDBParser(QUIET=True)

    # ---------------------------------------------------------------------
    # Low‑level helpers
    # ---------------------------------------------------------------------
    def _extract_ca(self, pdb_path: pathlib.Path):
        """Return (**coords**, **aa_list**) for all Cα atoms in the structure."""
        coords, aas = [], []
        structure = self.parser.get_structure("af", str(pdb_path))
        for model in structure:
            for chain in model:
                for res in chain:
                    if "CA" not in res:
                        continue
                    coords.append(torch.tensor(res["CA"].coord, dtype=torch.float32))
                    aas.append(res.get_resname().upper())
        return torch.stack(coords), aas

    def _aa_one_hot(self, aa_list):
        one_hot = torch.zeros((len(aa_list), NUM_AA), dtype=torch.float32)
        for i, aa3 in enumerate(aa_list):
            aa1 = self._three_to_one(aa3)
            idx = AA_TO_IDX.get(aa1, None)
            if idx is not None:
                one_hot[i, idx] = 1.0
        return one_hot

    @staticmethod
    def _three_to_one(resname: str) -> str:
        """Convert 3‑letter residue code to 1‑letter, fallback to 'X'."""
        mapping = {
            "ALA": "A", "CYS": "C", "ASP": "D", "GLU": "E", "PHE": "F",
            "GLY": "G", "HIS": "H", "ILE": "I", "LYS": "K", "LEU": "L",
            "MET": "M", "ASN": "N", "PRO": "P", "GLN": "Q", "ARG": "R",
            "SER": "S", "THR": "T", "VAL": "V", "TRP": "W", "TYR": "Y",
        }
        return mapping.get(resname, "X")

    def _charges(self, aa_list):
        charges = torch.zeros((len(aa_list), 1), dtype=torch.float32)
        for i, aa3 in enumerate(aa_list):
            aa1 = self._three_to_one(aa3)
            charges[i, 0] = AA_CHARGE.get(aa1, 0)
        return charges

    def _crop_to_pocket(self,
                        coords: torch.Tensor,
                        ligand_coords: torch.Tensor | None) -> tuple[torch.Tensor, torch.Tensor]:
        """Return the subset of `coords` within `pocket_radius` of *any* `ligand_coords`."""
        if ligand_coords is None or self.pocket_radius is None:
            m = torch.ones(coords.size(0), dtype=torch.bool)
            return coords, m
        dist = torch.cdist(coords, ligand_coords)          # [N_res, N_lig]
        m = (dist.min(dim=1).values < self.pocket_radius)   # [N_res]
        return coords[m], m
    
    def build(
        self,
        pdb_path: str | pathlib.Path,
        seq: Optional[str] = None,
        esm: Optional[torch.Tensor] = None,
        ligand_coords: Optional[torch.Tensor] = None,
    ) -> Data:
        """Construct a **torch_geometric** graph for the given PDB file.

        If *esm* is provided, it is concatenated to Cα coordinates (re‑creates
        the old 1 283‑D representation).  Otherwise the node feature is:

            one‑hot(20) + charge(1) + coords(3) = 24‑D
        """
        pdb_path = pathlib.Path(pdb_path)
        coords, aa3 = self._extract_ca(pdb_path)

        # --- pocket cropping ----------------------------------------------
        coords, mask = self._crop_to_pocket(coords, ligand_coords)
        aa3 = [a for a, keep in zip(aa3, mask) if keep]
        if esm is not None:
            if coords.shape[0] != esm[mask].shape[0]:
                raise ValueError("Seq/structure length mismatch for ESM embedding")
            node_x = torch.cat([esm[mask], coords], dim=1)
        else:
            if seq is not None and len(seq) != coords.shape[0]:
                raise ValueError("Seq/structure length mismatch for one‑hot pathway")
            one_hot = self._aa_one_hot(aa3)
            charges = self._charges(aa3)
            node_x = torch.cat([one_hot, charges, coords], dim=1)

        dist = torch.cdist(coords, coords)
        within = (dist < self.cutoff) & (dist > 0)
        idx = torch.arange(coords.shape[0] - 1, dtype=torch.long)
        within[idx, idx + 1] = True
        within[idx + 1, idx] = True

        edge_index = within.nonzero(as_tuple=False).t()
        edge_attr = dist[within].unsqueeze(-1)

        return Data(x=node_x, pos=coords, edge_index=edge_index, edge_attr=edge_attr)

    # ------------------------------------------------------------------
    # Caching helpers (optional)
    # ------------------------------------------------------------------
    def load(self, chembl_id: str) -> Data:
        path = self.graph_dir / f"{chembl_id}.pt"
        if not path.is_file():
            raise FileNotFoundError(f"Protein graph not found: {path}")
        return torch.load(path, map_location="cpu", weights_only=False)

    def save(self, chembl_id: str, data: Data):
        path = self.graph_dir / f"{chembl_id}.pt"
        torch.save(data, path)
