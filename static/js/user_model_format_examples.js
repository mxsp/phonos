/**
 * User Model Format Examples
 * Provides tab-based documentation for different output formats
 */

const UserModelFormatExamples = {
    // Switch format example tab
    switchFormatExample(format) {
        try {
            // Update tab active state
            document.querySelectorAll('.mb-format-tab, .format-tab').forEach(tab => {
                tab.classList.toggle('active', tab.getAttribute('data-format') === format);
            });

            // Update content safely
            const container = document.getElementById('format-examples-content');
            if (container) {
                // Clear existing content
                container.textContent = '';
                // Render new content using safe DOM methods
                const card = this._createFormatExampleCard(format);
                if (card) {
                    container.appendChild(card);
                } else {
                    console.error('[FormatExamples] Failed to create card for format:', format);
                    const errorMsg = document.createElement('p');
                    errorMsg.textContent = `Failed to load example for format: ${format}`;
                    errorMsg.style.color = 'var(--color-error)';
                    container.appendChild(errorMsg);
                }
            } else {
                console.warn('[FormatExamples] Container not found: format-examples-content');
            }
        } catch (error) {
            console.error('[FormatExamples] Error switching format:', error);
        }
    },

    // Create format example card using safe DOM methods
    _createFormatExampleCard(format) {
        const examples = this._getFormatExamples();
        const ex = examples[format];

        if (!ex) {
            const p = document.createElement('p');
            p.textContent = 'Example not found';
            return p;
        }

        const card = document.createElement('div');
        card.className = 'format-example-card';

        // Header
        const header = document.createElement('div');
        header.className = 'format-example-header';

        const icon = document.createElement('div');
        icon.className = 'format-example-icon';
        icon.style.background = `linear-gradient(135deg, ${ex.color}, ${ex.color}dd)`;
        icon.textContent = ex.icon;

        const info = document.createElement('div');
        info.className = 'format-example-info';
        const title = document.createElement('h4');
        title.className = 'format-example-title';
        title.textContent = ex.title;
        const desc = document.createElement('p');
        desc.className = 'format-example-desc';
        desc.textContent = ex.desc;
        info.appendChild(title);
        info.appendChild(desc);

        const tag = document.createElement('span');
        tag.className = 'format-tag';
        tag.style.color = ex.color;
        tag.style.background = `${ex.color}1f`;
        tag.textContent = format.toUpperCase();

        header.appendChild(icon);
        header.appendChild(info);
        header.appendChild(tag);
        card.appendChild(header);

        // Use Case Section
        card.appendChild(this._createSection('Use Case', ex.useCase));

        // Input Schema Section
        const schemaSection = this._createCodeSection('Example Input Schema', this._formatSchemaExample(ex.schema));
        card.appendChild(schemaSection);

        // Result Format Section
        const resultSection = this._createCodeSection('Example Result Format', ex.resultFormat);
        card.appendChild(resultSection);

        // Allowed Fields Section
        const fieldsSection = this._createFieldsSection('Allowed Output Fields', ex.fields);
        card.appendChild(fieldsSection);

        // Tip Box
        const tipBox = document.createElement('div');
        tipBox.className = 'format-example-tip';
        tipBox.style.background = `${ex.color}12`;
        tipBox.style.borderColor = `${ex.color}45`;

        const tipTitle = document.createElement('p');
        tipTitle.className = 'format-example-tip__title';
        tipTitle.textContent = 'Builder Tip';

        const tipText = document.createElement('p');
        tipText.className = 'format-example-tip__copy';
        tipText.textContent = 'Set ';

        const codeInline = document.createElement('code');
        codeInline.style.background = 'rgba(0,0,0,0.25)';
        codeInline.style.padding = '2px 6px';
        codeInline.style.borderRadius = '4px';
        codeInline.style.fontFamily = 'var(--font-mono)';
        codeInline.textContent = `output_format: "${format}"`;

        tipText.appendChild(codeInline);
        tipText.appendChild(document.createTextNode(' to keep preview rendering, downloads, and API consumers consistent.'));

        tipBox.appendChild(tipTitle);
        tipBox.appendChild(tipText);
        card.appendChild(tipBox);

        return card;
    },

    _createSection(title, content) {
        const section = document.createElement('div');
        section.className = 'format-example-section';

        const sectionTitle = document.createElement('div');
        sectionTitle.className = 'format-example-section-title';
        sectionTitle.textContent = title;

        const p = document.createElement('p');
        p.style.margin = '0';
        p.style.fontSize = '12px';
        p.style.color = 'var(--text-secondary)';
        p.style.lineHeight = '1.6';
        p.textContent = content;

        section.appendChild(sectionTitle);
        section.appendChild(p);
        return section;
    },

    _createCodeSection(title, code) {
        const section = document.createElement('div');
        section.className = 'format-example-section';

        const sectionTitle = document.createElement('div');
        sectionTitle.className = 'format-example-section-title';
        sectionTitle.textContent = title;

        const codeDiv = document.createElement('div');
        codeDiv.className = 'format-example-code';
        codeDiv.textContent = code;

        section.appendChild(sectionTitle);
        section.appendChild(codeDiv);
        return section;
    },

    _createFieldsSection(title, fields) {
        const section = document.createElement('div');
        section.className = 'format-example-section';

        const sectionTitle = document.createElement('div');
        sectionTitle.className = 'format-example-section-title';
        sectionTitle.textContent = title;

        const ul = document.createElement('ul');
        ul.className = 'format-example-list';

        fields.forEach(field => {
            const li = document.createElement('li');
            const code = document.createElement('code');
            code.style.background = 'rgba(0,0,0,0.25)';
            code.style.padding = '2px 6px';
            code.style.borderRadius = '4px';
            code.style.fontFamily = 'var(--font-mono)';
            code.style.fontSize = '11px';
            code.textContent = field;
            li.appendChild(code);
            ul.appendChild(li);
        });

        const note = document.createElement('p');
        note.style.fontSize = '11px';
        note.style.color = 'var(--text-secondary)';
        note.style.margin = '8px 0 0 0';

        const noteStrong = document.createElement('strong');
        noteStrong.textContent = 'Note:';
        note.appendChild(noteStrong);
        note.appendChild(document.createTextNode(' Sensitive fields (user_id, api_key, password, etc.) are automatically removed from all outputs.'));

        section.appendChild(sectionTitle);
        section.appendChild(ul);
        section.appendChild(note);
        return section;
    },

    _formatSchemaExample(schema) {
        const formatted = schema.map(field => {
            const parts = [`  "name": "${field.name}"`, `"type": "${field.type}"`, `"label": "${field.label}"`];
            if (field.required) parts.push('"required": true');
            if (field.default !== undefined) parts.push(`"default": ${JSON.stringify(field.default)}`);
            if (field.options) parts.push(`"options": ${JSON.stringify(field.options)}`);
            return `{\n    ${parts.join(',\n    ')}\n  }`;
        }).join(',\n  ');
        return `[\n  ${formatted}\n]`;
    },

    _getFormatExamples() {
        return {
            pdb: {
                title: 'PDB - Protein Structure',
                desc: 'Output 3D protein structures in PDB format for visualization and analysis',
                icon: 'PDB',
                color: '#2f6f99',
                useCase: 'Protein folding, structure prediction, molecular modeling',
                schema: [
                    { name: 'sequence', type: 'textarea', label: 'Protein Sequence', required: true },
                    { name: 'model_preset', type: 'select', label: 'Model Preset', options: ['monomer', 'multimer'] }
                ],
                resultFormat: `{
  "status": "completed",
  "result": {
    "structure_pdb": "ATOM      1  N   MET A   1     ...",
    "confidence_scores": {
      "plddt": [95.2, 94.8, 93.1, ...],
      "mean_plddt": 92.5
    }
  },
  "output_format": "pdb",
  "metrics": {
    "runtime_seconds": 45.2
  }
}`,
                fields: ['structure_pdb', 'structure', 'confidence_scores', 'plddt', 'model_quality']
            },
            json: {
                title: 'JSON - Structured Data',
                desc: 'Return structured data, predictions, or analysis results in JSON format',
                icon: 'JSON',
                color: '#2d9d78',
                useCase: 'ML predictions, data analysis, classification tasks',
                schema: [
                    { name: 'input_text', type: 'textarea', label: 'Input Text', required: true },
                    { name: 'threshold', type: 'number', label: 'Confidence Threshold', default: 0.5 }
                ],
                resultFormat: `{
  "status": "completed",
  "result": {
    "prediction": "positive",
    "confidence": 0.95,
    "classes": {
      "positive": 0.95,
      "negative": 0.05
    },
    "metadata": {
      "model_version": "1.0"
    }
  },
  "output_format": "json",
  "metrics": {
    "runtime_seconds": 2.3
  }
}`,
                fields: ['All non-sensitive fields allowed (wildcard)']
            },
            png: {
                title: 'PNG - Image Output',
                desc: 'Generate images, visualizations, or plots in PNG format (base64 encoded)',
                icon: 'PNG',
                color: '#d9822b',
                useCase: 'Image generation, data visualization, molecular graphics',
                schema: [
                    { name: 'description', type: 'text', label: 'Image Description', required: true },
                    { name: 'width', type: 'number', label: 'Width (px)', default: 1920 },
                    { name: 'height', type: 'number', label: 'Height (px)', default: 1080 }
                ],
                resultFormat: `{
  "status": "completed",
  "result": {
    "image_base64": "iVBORw0KGgoAAAANSUhEUgAA...",
    "width": 1920,
    "height": 1080,
    "format": "png"
  },
  "output_format": "png",
  "metrics": {
    "runtime_seconds": 8.5
  }
}`,
                fields: ['image_base64', 'image_data', 'width', 'height', 'format']
            },
            sdf: {
                title: 'SDF - Molecule Format',
                desc: 'Output small molecules, ligands, or docking poses in SDF format',
                icon: 'SDF',
                color: '#7157a5',
                useCase: 'Molecular docking, ligand design, drug discovery',
                schema: [
                    { name: 'protein_pdb', type: 'file', label: 'Protein Structure (PDB)', required: true },
                    { name: 'ligand_smiles', type: 'text', label: 'Ligand SMILES', required: true },
                    { name: 'num_poses', type: 'number', label: 'Number of Poses', default: 10 }
                ],
                resultFormat: `{
  "status": "completed",
  "result": {
    "structure_sdf": "...SDF content...",
    "binding_poses": [
      {
        "rank": 1,
        "score": -9.2,
        "structure": "...SDF..."
      }
    ],
    "scores": {
      "best_affinity": -9.2,
      "mean_affinity": -7.8
    }
  },
  "output_format": "sdf",
  "metrics": {
    "runtime_seconds": 120.5
  }
}`,
                fields: ['structure_sdf', 'molecules', 'ligands', 'binding_poses', 'scores']
            },
            csv: {
                title: 'CSV - Tabular Data',
                desc: 'Export data tables, results, or batch analysis in CSV format',
                icon: 'CSV',
                color: '#2f6f99',
                useCase: 'Batch processing, data export, tabular results',
                schema: [
                    { name: 'sequences', type: 'file', label: 'Input Sequences (FASTA)', required: true },
                    { name: 'include_headers', type: 'checkbox', label: 'Include Headers', default: true }
                ],
                resultFormat: `{
  "status": "completed",
  "result": {
    "csv_data": "id,sequence,score,class\\nseq_1,MKLA...,0.95,positive\\n...",
    "rows": 150,
    "columns": ["id", "sequence", "score", "class"]
  },
  "output_format": "csv",
  "metrics": {
    "runtime_seconds": 15.2
  }
}`,
                fields: ['csv_data', 'data', 'rows', 'columns']
            }
        };
    }
};

// Make it available globally immediately (before DOMContentLoaded)
(function() {
    // Ensure App exists
    if (typeof window.App === 'undefined') {
        window.App = {};
    }

    // Make the module accessible first
    window.UserModelFormatExamples = UserModelFormatExamples;

    // Register or enhance the function
    window.App.switchFormatExample = function(format) {
        UserModelFormatExamples.switchFormatExample(format);
    };

    console.log('[UserModelFormatExamples] Module loaded and registered');
})();
