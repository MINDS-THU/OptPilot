import React from 'react';
import { Cpu, Key, Settings } from 'lucide-react';
import { AIProvider } from '../services/geminiService';
import { ModelPreset } from '../types';

interface Props {
  provider: AIProvider;
  modelName: string;
  apiKey: string;
  apiKeyAvailable?: boolean;
  modelPresets: ModelPreset[];
  onProviderChange: (provider: AIProvider, modelName: string) => void;
  onModelNameChange: (modelName: string) => void;
  onApiKeyChange: (apiKey: string) => void;
}

export const ApiConfigPanel: React.FC<Props> = ({
  provider,
  modelName,
  apiKey,
  apiKeyAvailable = false,
  modelPresets,
  onProviderChange,
  onModelNameChange,
  onApiKeyChange
}) => {
  const providerPresets = modelPresets.filter(preset => preset.provider === provider);
  const keyLabel = provider === 'gemini' ? 'Gemini API Key' : 'OpenRouter API Key';
  const providerLabel = provider === 'gemini' ? 'Gemini' : 'OpenRouter';

  return (
    <div className="space-y-3 pb-4 border-b border-slate-100">
      <div className="flex items-center gap-2 text-slate-800 font-semibold text-sm">
        <Settings size={16} /> API Configuration
      </div>

      <div className="space-y-2">
        <div className="flex gap-2 bg-slate-100 p-1 rounded">
          <button
            onClick={() => onProviderChange('gemini', 'gemini-2.5-flash')}
            className={`flex-1 text-xs py-1 rounded transition-colors ${provider === 'gemini' ? 'bg-white shadow text-blue-600 font-medium' : 'text-slate-500 hover:text-slate-700'}`}
          >
            Gemini
          </button>
          <button
            onClick={() => onProviderChange('openai', 'openrouter/qwen/qwen3-coder')}
            className={`flex-1 text-xs py-1 rounded transition-colors ${provider === 'openai' ? 'bg-white shadow text-green-600 font-medium' : 'text-slate-500 hover:text-slate-700'}`}
          >
            OpenRouter
          </button>
        </div>

        <div className="relative">
          <div className="absolute inset-y-0 left-0 flex items-center pl-2 pointer-events-none">
            <Cpu size={14} className="text-slate-400" />
          </div>
          <input
            type="text"
            list={`model-presets-${provider}`}
            placeholder="Model Name"
            value={modelName}
            onChange={(e) => onModelNameChange(e.target.value)}
            className="w-full pl-8 pr-3 py-1.5 text-xs border border-slate-300 rounded focus:ring-2 focus:ring-blue-200 focus:border-blue-400 outline-none transition-all"
          />
          <datalist id={`model-presets-${provider}`}>
            {providerPresets.map(preset => (
              <option key={`${preset.provider}-${preset.model}`} value={preset.model}>
                {preset.label}
              </option>
            ))}
          </datalist>
        </div>

        <div className="space-y-1">
          <div className="relative">
            <div className="absolute inset-y-0 left-0 flex items-center pl-2 pointer-events-none">
              <Key size={14} className="text-slate-400" />
            </div>
            <input
              type="password"
              placeholder={provider === 'openai' && apiKeyAvailable ? 'Using backend OPENROUTER_API_KEY' : keyLabel}
              value={apiKey}
              onChange={(e) => onApiKeyChange(e.target.value)}
              className="w-full pl-8 pr-3 py-1.5 text-xs border border-slate-300 rounded focus:ring-2 focus:ring-blue-200 focus:border-blue-400 outline-none transition-all"
            />
          </div>
          {provider === 'openai' && apiKeyAvailable && !apiKey && (
            <p className="text-[10px] text-slate-500">
              {providerLabel} key is loaded on the backend from the local environment.
            </p>
          )}
        </div>
      </div>
    </div>
  );
};
