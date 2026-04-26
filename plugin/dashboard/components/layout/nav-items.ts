import {
  LayoutDashboard,
  MessageSquare,
  Users,
  BookOpen,
  Settings,
  type LucideIcon,
} from "lucide-react";

export interface NavItem {
  href: string;
  label: string;
  icon: LucideIcon;
}

export const navItems: NavItem[] = [
  { href: "/dashboard", label: "Dashboard", icon: LayoutDashboard },
  { href: "/sessions", label: "Sessions", icon: MessageSquare },
  { href: "/profiles", label: "Profiles", icon: Users },
  { href: "/playbooks", label: "Playbooks", icon: BookOpen },
  { href: "/configure", label: "Configure", icon: Settings },
];
