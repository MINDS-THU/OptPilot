import { ParsedStructure } from "../types";
import { AGENT_API_URL } from "./agentService";

export type AIProvider = 'openai';

export interface AIConfig {
  apiKey: string;
  provider: AIProvider;
  model: string;
}

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

  try {
    const localParsed = localParseXdevsCode(className, codeContent);
    if (localParsed) {
      console.info(`[Visualizer] Parsed ${className} locally: ${localParsed.components.length} components, ${localParsed.couplings.length} couplings.`);
      return localParsed;
    }

    if (provider !== 'openai') {
      throw new Error(`Unsupported provider: ${provider}`);
    }

    console.info(`[Visualizer] Calling backend ${model} for ${className}.`);
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

  } catch (error) {
    console.error("Error parsing model code:", error);
    throw error;
  }
};
