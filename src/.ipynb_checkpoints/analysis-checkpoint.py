import tkinter as tk
from tkinter import ttk

import matplotlib.pyplot as plt
import rdkit.Chem
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.colors import Normalize

from rdkit.Chem import AllChem
from rdkit.Chem.Draw import rdMolDraw2D

import pandas as pd
import numpy as np
import torch
import io
from PIL import Image

from utils.dataset import DrugProteinDataset
from utils.helper_functions import set_seeds
from model import GraphAttentionNetwork


class MoleculeViewer(tk.Tk):
    def __init__(self, data_path: str):
        super().__init__()

        self.title("CD4C Molecule Viewer")
        self.state("zoomed")
        # Use a white background.
        self.configure(bg="white")

        # Make the 2nd and 3rd column.
        self.grid_columnconfigure(1, weight=1)
        self.grid_columnconfigure(2, weight=1)
        self.grid_rowconfigure(0, weight=1)

        data_df, protein_embeddings_df = load_data(data_path)
        self.dataset = DrugProteinDataset(data_df, protein_embeddings_df)

        # TODO - remove hard-coded .pth file and model params
        self.model = GraphAttentionNetwork(
            "cpu",
            349,
            1,
            16,
            96,
            7,
            6,
            0.0,
            96
        ).to(torch.float32).to("cpu")
        self.model.load_state_dict(torch.load("../models/model.pth", weights_only=False, map_location=torch.device('cpu')))
        self.model.eval()

        # Create a dictionary that maps the pair of ChEMBL IDs to the index of that entry.
        self.pair_to_idx_mapping = {}
        # Create a dictionary that maps the protein's ChEMBL ID to the drugs tested for interaction with that protein.
        self.protein_to_drugs_mapping = {}

        for idx in data_df.index:
            protein_pchembl_id = data_df.at[idx, 'Target_ID']
            drug_pchembl_id = data_df.at[idx, 'ChEMBL_ID']
            self.pair_to_idx_mapping[(protein_pchembl_id, drug_pchembl_id)] = idx

            if protein_pchembl_id in self.protein_to_drugs_mapping:
                self.protein_to_drugs_mapping[protein_pchembl_id].append(drug_pchembl_id)
            else:
                self.protein_to_drugs_mapping[protein_pchembl_id] = [drug_pchembl_id]

        self._create_settings_frame()
        # Create a figure to show the drug molecule.
        self.fig, self.ax, self.canvas = self._init_canvas(column=1)
        self._create_info_frame()

    def _init_canvas(self, column: int, size: int = 1200, **kwargs) -> tuple[Figure, plt.Axes, FigureCanvasTkAgg]:
        fig = Figure()
        ax = fig.add_subplot()
        ax.set_xlim(0, size)
        ax.set_ylim(size, 0)  # Flip y-axis
        ax.set_xticks([])
        ax.set_yticks([])
        # Add a placeholder white image.
        ax.imshow(Image.new('RGB', (size, size), (255, 255, 255)))

        canvas = FigureCanvasTkAgg(fig, master=self)
        canvas.get_tk_widget().grid(row=0, column=column, sticky="nsew", padx=10, pady=10, **kwargs)

        return fig, ax, canvas

    def _create_settings_frame(self) -> None:
        # Use a larger font.
        font = ("Helvetica", 12)
        x_pad = 30

        # Create a left panel that contains various settings that the user can modify.
        self.settings_frame = tk.Frame(self)
        self.settings_frame.grid(row=0, column=0, padx=10, sticky="nsw")

        # Add a spacer at the top.
        tk.Label(self.settings_frame, text="", height=2).pack()

        tk.Label(self.settings_frame, text="Protein ChEMBL ID:", font=font).pack(padx=x_pad)
        self.protein_dropdown = ttk.Combobox(
            self.settings_frame, values=[""] + list(dict.fromkeys(self.dataset.protein_ids)),
            state="readonly", font=font)
        self.protein_dropdown.pack(pady=(5, 20), padx=x_pad)
        self.protein_dropdown.current(0)
        self.protein_dropdown.bind("<<ComboboxSelected>>", self._update_drug_dropdown)

        tk.Label(self.settings_frame, text="Drug ChEMBL ID:", font=font).pack(padx=x_pad)
        self.drug_dropdown = ttk.Combobox(self.settings_frame, state="readonly", font=font)
        self.drug_dropdown.pack(pady=(5, 20), padx=x_pad)
        self._update_drug_dropdown()

        self.submit_button = tk.Button(self.settings_frame, text="Draw Molecule", font=font,
                                       command=self._update_display)
        self.submit_button.pack(padx=x_pad)

    def _create_info_frame(self) -> None:
        self.info_frame = tk.Frame(self, width=400, padx=10, pady=10)
        self.info_frame.grid_propagate(False)
        self.info_frame.grid(row=0, column=2, sticky="ns", padx=50, pady=100)

        self._update_info_frame("", "", "")

    def _update_info_frame(self, actual_pchembl_str: str, pred_pchembl_str: str, smiles_str: str) -> None:
        for widget in self.info_frame.winfo_children():
            widget.destroy()

        data = [
            ("Protein ID", self.protein_dropdown.get(), False),
            ("Drug ID", self.drug_dropdown.get(), False),
            ("Drug SMILES String", smiles_str, True),
            ("", "", False),  # Add an extra label for padding
            ("Actual pChEMBL", actual_pchembl_str, False),
            ("Predicted pChEMBL", pred_pchembl_str, False),
        ]

        row = 0
        for label, value, newline in data:
            label = tk.Message(self.info_frame, text=label, font=("Arial", 10, "bold"), anchor="w",
                               width=280 if newline else 140)
            label.grid(row=row, column=0, sticky="w", padx=5, pady=2, columnspan=2 if newline else 1)

            if newline:
                value = tk.Message(self.info_frame, text=value, font=("Arial", 10), anchor="w", width=280)
                value.grid(row=row + 1, column=0, sticky="w", padx=5, pady=2, columnspan=2)
                row += 2
            else:
                value = tk.Message(self.info_frame, text=value, font=("Arial", 10), anchor="w", width=140)
                value.grid(row=row, column=1, sticky="w", padx=5, pady=2)
                row += 1

    def _update_drug_dropdown(self, event: tk.Event = None) -> None:
        selected_protein = self.protein_dropdown.get()
        if selected_protein == "":
            self.drug_dropdown["values"] = [""]
            self.drug_dropdown.current(0)
        else:
            new_drug_options = self.protein_to_drugs_mapping[selected_protein]
            self.drug_dropdown["values"] = [""] + new_drug_options
            if self.drug_dropdown.get() not in new_drug_options:
                self.drug_dropdown.current(0)

    def _update_display(self) -> None:
        protein_chembl_id = self.protein_dropdown.get()
        drug_chembl_id = self.drug_dropdown.get()

        # Can only create a molecule graph if both the protein and the drug are specified.
        if protein_chembl_id == "" or drug_chembl_id == "":
            return

        idx = self.pair_to_idx_mapping[(protein_chembl_id, drug_chembl_id)]
        node_contributions, pred_pchembl = self._get_node_contributions(idx)
        mol = self.dataset.drug_graphs[idx].mol
        actual_pchembl = self.dataset.pchembl_scores[idx]
        drug_smiles_str = self.dataset.smiles_strs[idx]

        self._draw_molecule(mol, node_contributions)
        self._update_info_frame(str(round(actual_pchembl, 2)), str(round(pred_pchembl, 2)), drug_smiles_str)

    def _draw_molecule(self, mol: rdkit.Chem.Mol, node_contributions: list[float],
                       contribution_scaler: float = 0.3) -> None:
        # Generate 2D coordinates for the molecule.
        AllChem.Compute2DCoords(mol)

        # Create a molecule drawing.
        drawer = rdMolDraw2D.MolDraw2DCairo(1200, 1200)
        drawer.DrawMolecule(mol)
        drawer.FinishDrawing()

        # Convert the drawing to a PIL Image.
        png_data = drawer.GetDrawingText()
        image = Image.open(io.BytesIO(png_data))
        self.ax.imshow(image)

        for i in range(mol.GetNumAtoms()):
            # Get the coordinates for the ith atom.
            x, y = drawer.GetDrawCoords(i)
            # Calculate the scaled contribution of this atom
            scaled_contribution = max(-1.0, min(1.0, node_contributions[i] * contribution_scaler))

            color_scheme = 'Reds' if scaled_contribution < 0 else 'Greens'
            # Create a circle around this atom to show its relative contribution to the interaction strength.
            self._create_gradient_circle(x, y, 200, color_scheme, center_alpha=abs(scaled_contribution))

        self.canvas.draw()

    def _create_gradient_circle(self, x: int, y: int, radius: int, color_scheme: str,
                                center_alpha: float = 0.3, edge_alpha: float = 0) -> None:
        # Create a grid for the gradient.
        grid_size = 500
        x_vals = np.linspace(x - radius, x + radius, grid_size)
        y_vals = np.linspace(y - radius, y + radius, grid_size)
        x_grid, y_grid = np.meshgrid(x_vals, y_vals)

        # Compute distances from the center of the circle.
        dist_from_center = np.sqrt((x_grid - x) ** 2 + (y_grid - y) ** 2)

        # Linearly scale the distances to be from 0 to the radius.
        norm = Normalize(vmin=0, vmax=radius)
        gradient = norm(dist_from_center)
        # The center should have the most intense color, fading outwars from there.
        gradient = np.clip(1 - gradient, 0, 1)

        # Fetch the specified color map.
        color_map = plt.get_cmap(color_scheme)
        rgba_colors = color_map(gradient)

        # Adjust the alpha (opacity) channel to fade out towards the edge.
        alpha_gradient = np.clip(center_alpha * gradient + edge_alpha * (1 - gradient), edge_alpha, center_alpha)
        rgba_colors[..., 3] = alpha_gradient

        # Overlay the gradient circle.
        self.ax.imshow(
            rgba_colors,
            extent=(x - radius, x + radius, y - radius, y + radius),
            origin='lower',
            interpolation='bilinear'
        )

    def _get_node_contributions(self, idx: int) -> tuple[list[float], float]:
        node_features, edge_features, adjacency_matrix, pchembl_score = self.dataset[idx]
        # Count the number of atoms in the drug molecule (excluding atoms that were added for padding).
        num_real_nodes = (adjacency_matrix.sum(dim=1) != 0).sum().item()

        # Add an extra dimension to allow for masking one node in each sample.
        node_features = node_features.unsqueeze(0).repeat(num_real_nodes + 1, 1, 1)
        edge_features = edge_features.repeat(num_real_nodes + 1, 1, 1, 1)
        adjacency_matrix = adjacency_matrix.repeat(num_real_nodes + 1, 1, 1)

        for i in range(num_real_nodes):
            # Mask the node features for the ith node.
            node_features[i, i, :] = 0.0
            # Mask the edge features for edges connecting to the ith node.
            edge_features[i, i, :, :] = 0.0
            edge_features[i, :, i, :] = 0.0
            # Mask the edges connecting to the ith node.
            adjacency_matrix[i, i, :] = 0.0
            adjacency_matrix[i, :, i] = 0.0

        preds = self.model(node_features, edge_features, adjacency_matrix).squeeze(-1).tolist()
        # The real pChEMBL prediction uses the whole drug graph without masking any nodes.
        real_pred = preds.pop()
        node_contributions = []
        for x in preds:
            # The contribution of a node is given by the change in the pChEMBL prediction when that
            # node is masked (e.g. if the prediction drops when the node is masked, then the node has
            # a positive contribution).
            node_contributions.append(real_pred - x)

        return node_contributions, real_pred


def load_data(data_path: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    # TODO - change to using filtered_cancer_all.csv dataset
    data_df = pd.read_csv(f'{data_path}/filtered_cancer_small.csv')
    protein_embeddings_df = pd.read_csv(f'{data_path}/protein_embeddings.csv', index_col=0)
    return data_df, protein_embeddings_df


if __name__ == '__main__':
    set_seeds()
    viewer = MoleculeViewer('../data')
    viewer.mainloop()
