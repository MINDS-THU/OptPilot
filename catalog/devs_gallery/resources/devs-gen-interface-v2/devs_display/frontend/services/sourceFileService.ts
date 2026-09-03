import { FileMap, SystemModelInfo } from '../types';

export const normalizeFilePath = (path: string) => path.replace(/\\/g, '/').replace(/^\.?\//, '').replace(/^\/+/, '');

export const resolveClassSourcePath = (
  className: string,
  modelInfo: SystemModelInfo | null,
  files: FileMap
): string | null => {
  const metadataPath = modelInfo?.[className]?.path;
  const entries = Object.entries(files);
  if (metadataPath) {
    const normalizedMetadataPath = normalizeFilePath(metadataPath);
    const match = entries.find(([path]) => {
      const normalizedPath = normalizeFilePath(path);
      return normalizedPath === normalizedMetadataPath
        || normalizedPath.endsWith(`/${normalizedMetadataPath}`)
        || normalizedMetadataPath.endsWith(`/${normalizedPath}`);
    });
    if (match) return match[0];

    const filename = normalizedMetadataPath.split('/').pop();
    const filenameMatch = entries.find(([path]) => normalizeFilePath(path).split('/').pop() === filename);
    if (filenameMatch) return filenameMatch[0];
  }

  const escaped = className.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const classRegex = new RegExp(`^class\\s+${escaped}\\b`, 'm');
  return entries.find(([path, content]) => path.endsWith('.py') && classRegex.test(content))?.[0] || null;
};

export const getKeyModuleFilePaths = (modelInfo: SystemModelInfo | null, files: FileMap): string[] => {
  if (!modelInfo) return [];
  return Array.from(new Set(
    Object.keys(modelInfo)
      .map(className => resolveClassSourcePath(className, modelInfo, files))
      .filter((path): path is string => Boolean(path))
  ));
};

export const isKnownNoiseFile = (path: string): boolean => {
  const normalized = normalizeFilePath(path);
  const parts = normalized.split('/');
  const filename = parts[parts.length - 1] || normalized;
  return parts.some(part => [
    '__pycache__', '.pytest_cache', 'node_modules', 'dist', '.git',
    '_analysis_logs', 'working_dirs', 'persistent_storage', 'index_dir'
  ].includes(part))
    || filename === '.DS_Store'
    || /\.(pyc|pyo|pyd|so|log|tmp|temp|bak|swp|swo|cache)$/i.test(filename);
};

export const isDisplayableSourceFile = (path: string): boolean => {
  return /\.(py|json|md|txt|ya?ml|toml|sh)$/i.test(normalizeFilePath(path));
};

export const sortSourceFiles = (paths: string[], keyModuleFilePaths: string[] = []): string[] => {
  const keyPaths = new Set(keyModuleFilePaths.map(normalizeFilePath));
  return [...paths].sort((a, b) => {
    const aKey = keyPaths.has(normalizeFilePath(a)) ? 0 : 1;
    const bKey = keyPaths.has(normalizeFilePath(b)) ? 0 : 1;
    const aPython = a.endsWith('.py') ? 0 : 1;
    const bPython = b.endsWith('.py') ? 0 : 1;
    return aKey - bKey || aPython - bPython || normalizeFilePath(a).localeCompare(normalizeFilePath(b));
  });
};
