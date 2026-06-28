import { GoogleGenAI, Type } from "@google/genai";
import { ParsedStructure } from "../types";
import { AGENT_API_URL } from "./agentService";

export type AIProvider = 'gemini' | 'openai';

export interface AIConfig {
  apiKey: string;
  provider: AIProvider;
  model: string;
}

// Helper to clean JSON string if the model returns markdown code blocks
const cleanJson = (text: string): string => {
  let clean = text.trim();
  if (clean.startsWith('```json')) {
    clean = clean.replace(/^```json/, '').replace(/```$/, '');
  } else if (clean.startsWith('```')) {
    clean = clean.replace(/^```/, '').replace(/```$/, '');
  }
  return clean;
};

const SYSTEM_INSTRUCTION = `
    You are an expert Python Static Analysis tool for xDEVS simulation models.
    Your task is to analyze the provided Python class definition of a DEVS Coupled Model to extract its internal structure.
    
    ### 1. Sub-components
    Find all \`self.add_component(model)\` calls.
    - Identify the **Instance Name** (the name given to the submodel, e.g., "generator" or "server_0").
    - Identify the **Class Name** (the Python class instantiated, e.g., "Generator" or "Server").
    - **Loops**: If components are created in a loop (e.g., \`for i in range(num_workers):\`), assume \`num_workers=2\` (or any variable range) and generate concrete instances (e.g., "worker_0", "worker_1").
    
    ### 2. Couplings
    Find all \`self.add_coupling(source, target)\` calls.
    - The \`source\` and \`target\` arguments typically refer to a model instance's port. 
      - Syntax might be: \`model_instance.output["port_name"]\`, \`model_instance.ports.out["port_name"]\`, or similar.
    - **Source**: Extract { source_model, source_port }. 
      - If \`source\` refers to \`self\` (e.g. \`self.input["in"]\`), then \`source_model\` is "self".
      - Otherwise, \`source_model\` is the instance name of the sub-component.
    - **Target**: Extract { target_model, target_port }.
      - If \`target\` refers to \`self\` (e.g. \`self.output["out"]\`), then \`target_model\` is "self".
      - Otherwise, \`target_model\` is the instance name of the sub-component.
    - **Loops**: If couplings are inside a loop, expand them for the concrete instances generated in step 1.
      - Example: \`self.add_coupling(A, B[i])\` inside \`range(2)\` -> generate coupling (A -> B_0) and (A -> B_1).
      - Example: \`self.add_coupling(B[i], B[i+1])\` -> generate (B_0 -> B_1).

    ### Output Format
    Return ONLY valid JSON matching the following structure:
    {
      "components": [ {"name": "string", "className": "string"} ],
      "couplings": [ {"source_model": "string", "source_port": "string", "target_model": "string", "target_port": "string"} ]
    }
`;

const getPrompt = (className: string, codeContent: string) => `
    Analyze the following Python code for class '${className}'.
    
    Context:
    - This is a generic DEVS model.
    - If constructor arguments define counts use 2 as the default value to instantiate sub-components.
    - Strictly map the coupling logic to the instantiated components.

    Code:
    ${codeContent}
`;

const extractClassBody = (className: string, codeContent: string): string => {
  const classMatch = new RegExp(`^class\\s+${className}\\s*\\([^\\n]*\\):`, 'm').exec(codeContent);
  if (!classMatch) return codeContent;
  const start = classMatch.index;
  const nextClass = /^class\s+\w+\s*\([^\n]*\):/gm;
  nextClass.lastIndex = start + classMatch[0].length;
  const nextMatch = nextClass.exec(codeContent);
  return codeContent.slice(start, nextMatch ? nextMatch.index : codeContent.length);
};

const extractPorts = (body: string, direction: 'input' | 'output'): string[] => {
  const ports = new Set<string>();
  const portMethod = direction === 'input' ? 'add_in_port' : 'add_out_port';
  const portCall = new RegExp(`${portMethod}\\(\\s*Port\\([^,]+,\\s*["']([^"']+)["']`, 'g');
  for (const match of body.matchAll(portCall)) ports.add(match[1]);

  const bagAccess = new RegExp(`self\\.${direction}\\[["']([^"']+)["']\\]`, 'g');
  for (const match of body.matchAll(bagAccess)) ports.add(match[1]);
  return Array.from(ports);
};

const localParseXdevsCode = (className: string, codeContent: string): ParsedStructure | null => {
  const body = extractClassBody(className, codeContent);
  const assignments = new Map<string, { className: string; instanceName: string }>();
  const lines = body.split('\n');

  for (let idx = 0; idx < lines.length; idx += 1) {
    const assignMatch = /^\s*(\w+)\s*=\s*(\w+)\s*\(/.exec(lines[idx]);
    if (!assignMatch) continue;

    const [, variableName, assignedClass] = assignMatch;
    const callText = lines.slice(idx, Math.min(lines.length, idx + 12)).join('\n');
    const nameMatch = /name\s*=\s*["']([^"']+)["']/.exec(callText);
    assignments.set(variableName, {
      className: assignedClass,
      instanceName: nameMatch?.[1] || variableName
    });
  }

  const components = Array.from(body.matchAll(/self\.add_component\((\w+)\)/g))
    .map(match => {
      const variableName = match[1];
      const assignment = assignments.get(variableName);
      return assignment
        ? { name: assignment.instanceName, className: assignment.className }
        : { name: variableName, className: variableName };
    });

  for (const match of body.matchAll(/self\.add_component\(\s*(\w+)\(([\s\S]*?)\)\s*\)/g)) {
    const classNameInline = match[1];
    const nameMatch = /name\s*=\s*["']([^"']+)["']/.exec(match[2]);
    components.push({
      name: nameMatch?.[1] || classNameInline,
      className: classNameInline
    });
  }

  const endpointPattern = /(self|\w+)\.(?:input|output)\[["']([^"']+)["']\]/;
  const endpointToModelPort = (endpoint: string): { model: string; port: string } | null => {
    const match = endpointPattern.exec(endpoint);
    if (!match) return null;
    const objectName = match[1];
    return {
      model: objectName === 'self' ? 'self' : (assignments.get(objectName)?.instanceName || objectName),
      port: match[2]
    };
  };

  const couplings = [];
  for (const match of body.matchAll(/self\.add_coupling\(([^,\n]+),\s*([^)]+)\)/g)) {
    const source = endpointToModelPort(match[1]);
    const target = endpointToModelPort(match[2]);
    if (source && target) {
      couplings.push({
        source_model: source.model,
        source_port: source.port,
        target_model: target.model,
        target_port: target.port
      });
    }
  }

  if (components.length === 0 && couplings.length === 0) return null;
  return { components, couplings };
};

export const parseModelCode = async (
  className: string,
  codeContent: string,
  config: AIConfig
): Promise<ParsedStructure> => {
  const { apiKey, provider, model } = config;

  const prompt = getPrompt(className, codeContent);

  try {
    const localParsed = localParseXdevsCode(className, codeContent);
    if (localParsed) {
      console.info(`[Visualizer] Parsed ${className} locally: ${localParsed.components.length} components, ${localParsed.couplings.length} couplings.`);
      return localParsed;
    }

    console.info(`[Visualizer] Calling ${provider} model ${model} for ${className}. Prompt:`, prompt);
    let jsonText = "";

    if (provider === 'gemini') {
      if (!apiKey) throw new Error("Gemini API Key is required");
      const ai = new GoogleGenAI({ apiKey });
      const response = await ai.models.generateContent({
        model: model || "gemini-2.5-flash",
        contents: prompt,
        config: {
          systemInstruction: SYSTEM_INSTRUCTION,
          responseMimeType: "application/json",
          // Gemini Schema is strict and helpful
          responseSchema: {
            type: Type.OBJECT,
            properties: {
              components: {
                type: Type.ARRAY,
                items: {
                  type: Type.OBJECT,
                  properties: {
                    name: { type: Type.STRING },
                    className: { type: Type.STRING }
                  },
                  required: ["name", "className"]
                }
              },
              couplings: {
                type: Type.ARRAY,
                items: {
                  type: Type.OBJECT,
                  properties: {
                    source_model: { type: Type.STRING },
                    source_port: { type: Type.STRING },
                    target_model: { type: Type.STRING },
                    target_port: { type: Type.STRING }
                  },
                  required: ["source_model", "source_port", "target_model", "target_port"]
                }
              }
            },
            required: ["components", "couplings"]
          }
        }
      });
      jsonText = response.text || "";

    } else if (provider === 'openai') {
      const response = await fetch(`${AGENT_API_URL}/visualizer/parse-model`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          class_name: className,
          code_content: codeContent,
          provider,
          model: model || "openrouter/openai/gpt-5.4-mini",
          api_key: apiKey || null
        })
      });

      if (!response.ok) {
        const errorData = await response.json().catch(() => ({}));
        throw new Error(`OpenRouter Error: ${response.status} ${errorData.detail || response.statusText}`);
      }

      const data = await response.json();
      return data.parsed as ParsedStructure;
    } else {
      throw new Error(`Unsupported provider: ${provider}`);
    }

    if (!jsonText) throw new Error("Empty response from AI");
    
    return JSON.parse(cleanJson(jsonText)) as ParsedStructure;

  } catch (error) {
    console.error("Error parsing model code:", error);
    throw error;
  }
};
