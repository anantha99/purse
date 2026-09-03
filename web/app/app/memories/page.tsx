import MemoriesScreen from "@/components/screens/MemoriesScreen";

export default function MemoriesPage() {
  const mcpUrl = process.env.PURSE_MCP_URL || "https://your-vault.dev/mcp";
  return <MemoriesScreen mcpUrl={mcpUrl} />;
}
